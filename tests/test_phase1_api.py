import base64
import logging

import pytest
from fastapi.testclient import TestClient

from src.app.api import create_app
from src.config import Settings
from src.security import provision_api_key
from src.velocity import VelocityReadiness, VelocityUnavailable


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def secured_settings(**overrides):
    values = {
        "environment": "test",
        "auth_mode": "required",
        "api_key_peppers": {"v1": encoded_secret()},
        "data_pepper": encoded_secret(b"d"),
        "velocity_warmup_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def valid_payment(transaction_id="txn-phase1-1", **overrides):
    document = {
        "transaction_id": transaction_id,
        "occurred_at": "2026-09-01T09:30:00+03:00",
        "amount": 125.5,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
        "identifiers": {
            "payment_method_token": "pmt_tok_phase1",
            "customer_id": "cus_tok_phase1",
        },
    }
    document.update(overrides)
    return document


@pytest.fixture
def secured_api():
    settings = secured_settings()
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug="pilot-a",
        name="Pilot A",
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="integration",
        scopes=["decisions:write", "metrics:read"],
    )
    with TestClient(app) as client:
        yield client, app, settings, tenant_id, issued


def authorization(plaintext):
    return {"Authorization": f"Bearer {plaintext}"}


def test_decision_endpoint_requires_a_valid_bearer_key(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    missing = client.post("/v1/decisions", json=valid_payment())
    malformed = client.post(
        "/v1/decisions",
        json=valid_payment(),
        headers={"Authorization": "Bearer not-a-key"},
    )
    assert missing.status_code == malformed.status_code == 401
    assert missing.json()["detail"] == malformed.json()["detail"]
    assert missing.headers["WWW-Authenticate"] == "Bearer"

    accepted = client.post(
        "/v1/decisions",
        json=valid_payment(),
        headers=authorization(issued.plaintext),
    )
    assert accepted.status_code == 200


def test_identical_replay_is_byte_exact_and_has_one_stable_decision(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = authorization(issued.plaintext)
    first = client.post("/v1/decisions", json=valid_payment(), headers=headers)
    replay = client.post("/v1/decisions", json=valid_payment(), headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content
    assert first.headers["X-Decision-Replayed"] == "false"
    assert replay.headers["X-Decision-Replayed"] == "true"
    assert first.json()["decision_id"] == replay.json()["decision_id"]


def test_terminal_decision_failure_is_durable_and_replayed_exactly(
    secured_api, monkeypatch
):
    client, _app, _settings, _tenant_id, issued = secured_api

    def fail_evaluation(*_args, **_kwargs):
        raise RuntimeError("internal details must not escape")

    monkeypatch.setattr("src.service.decide_payment", fail_evaluation)
    headers = authorization(issued.plaintext)
    request = valid_payment("txn-terminal-failure")
    first = client.post("/v1/decisions", json=request, headers=headers)
    replay = client.post("/v1/decisions", json=request, headers=headers)

    assert first.status_code == replay.status_code == 500
    assert first.content == replay.content
    assert first.headers["X-Decision-Replayed"] == "false"
    assert replay.headers["X-Decision-Replayed"] == "true"
    assert first.json()["detail"]["code"] == "decision_evaluation_failed"
    assert "internal details" not in first.text


def test_changed_payload_under_same_transaction_is_409(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = authorization(issued.plaintext)
    assert (
        client.post("/v1/decisions", json=valid_payment(), headers=headers).status_code
        == 200
    )
    conflict = client.post(
        "/v1/decisions",
        json=valid_payment(amount=999),
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_same_transaction_id_is_isolated_between_tenants(secured_api):
    client, app, settings, _tenant_id, first_key = secured_api
    second_tenant = app.state.runtime.credentials.create_tenant(
        slug="pilot-b",
        name="Pilot B",
    )
    second_key = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=second_tenant,
        name="integration",
        scopes=["decisions:write"],
    )
    first = client.post(
        "/v1/decisions",
        json=valid_payment(),
        headers=authorization(first_key.plaintext),
    )
    second = client.post(
        "/v1/decisions",
        json=valid_payment(),
        headers=authorization(second_key.plaintext),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["decision_id"] != second.json()["decision_id"]


def test_scope_enforcement_distinguishes_401_from_403(secured_api):
    client, app, settings, tenant_id, _issued = secured_api
    metrics_key = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="metrics",
        scopes=["metrics:read"],
    )
    headers = authorization(metrics_key.plaintext)
    forbidden = client.post("/v1/decisions", json=valid_payment(), headers=headers)
    assert forbidden.status_code == 403
    assert client.get("/metrics", headers=headers).status_code == 200


def test_velocity_rule_fires_on_the_tenth_server_observation(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = authorization(issued.plaintext)
    responses = [
        client.post(
            "/v1/decisions",
            json=valid_payment(transaction_id=f"txn-velocity-{number}", amount=1),
            headers=headers,
        )
        for number in range(1, 11)
    ]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["decision"] == "approve" for response in responses[:9])
    assert responses[9].json()["decision"] == "decline"
    assert responses[9].json()["rule_hits"][0]["rule"] == "trusted_velocity_burst"


def test_client_velocity_and_tenant_spoofing_remain_invalid(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = {
        **authorization(issued.plaintext),
        "X-Tenant-ID": "attacker-tenant",
    }
    for extra in (
        {"tx_count_last_60s": 100},
        {"tenant_id": "attacker-tenant"},
        {"context": {"max_count_60s": 100}},
    ):
        request = valid_payment()
        request.update(extra)
        assert (
            client.post("/v1/decisions", json=request, headers=headers).status_code
            == 422
        )


def test_validation_errors_never_reflect_sensitive_request_values(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    request = valid_payment()
    request["cvv"] = "cvv-must-never-be-reflected"
    response = client.post(
        "/v1/decisions",
        json=request,
        headers=authorization(issued.plaintext),
    )
    assert response.status_code == 422
    assert "sensitive payment credential fields are not accepted" in response.text
    assert "cvv-must-never-be-reflected" not in response.text
    assert all(
        "input" not in error and "ctx" not in error
        for error in response.json()["detail"]
    )


def test_liveness_is_public_but_metrics_and_legacy_score_are_protected(secured_api):
    client, _app, _settings, _tenant_id, _issued = secured_api
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/metrics").status_code == 401
    features = {f"V{number}": 0 for number in range(1, 29)}
    features.update({"Time": 0, "Amount": 1})
    assert client.post("/score", json={"features": features}).status_code == 401


def test_global_metrics_and_legacy_score_are_not_mounted_outside_local_test(
    secured_api,
):
    _client, test_app, _settings, _tenant_id, _issued = secured_api
    production = secured_settings(
        environment="production",
        database_url="postgresql+psycopg://app:secret@db/fraudguard",
        auto_create_schema=False,
        velocity_backend="redis",
        velocity_failure_mode="fail_closed",
        redis_url="redis://redis:6379/0",
        velocity_warmup_seconds=300,
    )
    production_app = create_app(production, runtime=test_app.state.runtime)
    paths = production_app.openapi()["paths"]
    assert "/metrics" not in paths
    assert "/score" not in paths
    assert "/v1/decisions" in paths


def test_metrics_have_no_tenant_or_transaction_labels(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = authorization(issued.plaintext)
    client.post(
        "/v1/decisions",
        json=valid_payment("txn-metrics-secret"),
        headers=headers,
    )
    metrics = client.get("/metrics", headers=headers)
    assert metrics.status_code == 200
    assert "fraudguard_decisions_total" in metrics.text
    assert "txn-metrics-secret" not in metrics.text
    assert "pmt_tok_phase1" not in metrics.text


def test_request_size_and_request_id_boundary(secured_api):
    client, _app, _settings, _tenant_id, issued = secured_api
    headers = {
        **authorization(issued.plaintext),
        "Content-Type": "application/json",
        "X-Request-ID": "request-pilot-123",
    }
    normal = client.post(
        "/v1/decisions",
        json=valid_payment("txn-request-id"),
        headers=headers,
    )
    assert normal.headers["X-Request-ID"] == "request-pilot-123"

    too_large = client.post(
        "/v1/decisions",
        content=b"x" * 70_000,
        headers=headers,
    )
    assert too_large.status_code == 413
    assert too_large.json()["detail"]["code"] == "request_too_large"


def test_authorization_secret_never_enters_request_log(secured_api, caplog):
    client, _app, _settings, _tenant_id, issued = secured_api
    with caplog.at_level(logging.INFO, logger="fraudguard.api"):
        response = client.post(
            "/v1/decisions",
            json=valid_payment("txn-log-safety"),
            headers=authorization(issued.plaintext),
        )
    assert response.status_code == 200
    assert issued.plaintext not in caplog.text
    assert "txn-log-safety" not in caplog.text


class FailingVelocity:
    def observe(self, _event):
        raise VelocityUnavailable("offline")

    def readiness(self):
        return VelocityReadiness(
            ready=False,
            backend="test",
            production_safe=False,
            code="offline",
        )


@pytest.mark.parametrize(
    ("failure_mode", "expected_status", "expected_feature_status"),
    [
        ("fail_closed", 503, None),
        ("degraded_shadow", 200, "unavailable"),
    ],
)
def test_velocity_failure_behavior_is_explicit(
    failure_mode,
    expected_status,
    expected_feature_status,
):
    settings = secured_settings(velocity_failure_mode=failure_mode)
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug=f"pilot-{failure_mode}",
        name="Pilot",
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="integration",
        scopes=["decisions:write"],
    )
    app.state.runtime.service._velocity = FailingVelocity()
    with TestClient(app) as client:
        response = client.post(
            "/v1/decisions",
            json=valid_payment("txn-velocity-failure"),
            headers=authorization(issued.plaintext),
        )
    assert response.status_code == expected_status
    if expected_feature_status is None:
        assert response.json()["detail"]["code"] == "velocity_unavailable"
        assert response.headers["Retry-After"] == "1"
    else:
        assert response.json()["feature_status"] == expected_feature_status
        assert response.json()["decision"] == "approve"
