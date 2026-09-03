import base64

from fastapi.testclient import TestClient

from src.app.api import create_app
from src.config import Settings
from src.rate_limit import RateLimitUnavailable
from src.security import provision_api_key


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def settings_with_quota(*, burst=2):
    return Settings(
        environment="test",
        auth_mode="required",
        api_key_peppers={"v1": encoded_secret()},
        data_pepper=encoded_secret(b"d"),
        velocity_warmup_seconds=0,
        decision_rate_limit_per_second=1,
        decision_rate_limit_burst=burst,
    )


def payment(transaction_id):
    return {
        "transaction_id": transaction_id,
        "occurred_at": "2026-09-01T09:30:00+03:00",
        "amount": 125.5,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
        "identifiers": {
            "payment_method_token": "pmt_tok_phase1_rate",
            "customer_id": "cus_tok_phase1_rate",
        },
    }


def app_with_key(*, burst=2):
    settings = settings_with_quota(burst=burst)
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug=f"pilot-rate-{burst}",
        name="Pilot Rate Limit",
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="integration",
        scopes=["decisions:write"],
    )
    return app, issued.plaintext


def authorization(key):
    return {"Authorization": f"Bearer {key}"}


def test_tenant_quota_returns_bounded_headers_and_429():
    app, key = app_with_key(burst=2)
    headers = authorization(key)
    with TestClient(app) as client:
        first = client.post("/v1/decisions", json=payment("txn-rate-1"), headers=headers)
        second = client.post("/v1/decisions", json=payment("txn-rate-2"), headers=headers)
        rejected = client.post(
            "/v1/decisions", json=payment("txn-rate-3"), headers=headers
        )
    assert first.status_code == second.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert second.headers["X-RateLimit-Remaining"] == "0"
    assert rejected.status_code == 429
    assert rejected.json()["detail"]["code"] == "rate_limit_exceeded"
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.headers["X-RateLimit-Remaining"] == "0"


def test_authentication_failure_does_not_consume_tenant_quota():
    app, key = app_with_key(burst=1)
    with TestClient(app) as client:
        assert client.post("/v1/decisions", json=payment("unauthenticated")).status_code == 401
        accepted = client.post(
            "/v1/decisions",
            json=payment("txn-auth-before-rate"),
            headers=authorization(key),
        )
    assert accepted.status_code == 200
    assert accepted.headers["X-RateLimit-Remaining"] == "0"


class FailingRateLimiter:
    def consume(self, _tenant_id):
        raise RateLimitUnavailable("offline")


def test_rate_limit_backend_failure_is_fail_closed():
    app, key = app_with_key(burst=2)
    app.state.runtime.rate_limiter = FailingRateLimiter()
    with TestClient(app) as client:
        response = client.post(
            "/v1/decisions",
            json=payment("txn-rate-limit-offline"),
            headers=authorization(key),
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rate_limit_unavailable"
    assert response.headers["Retry-After"] == "1"


def test_rate_metrics_never_expose_tenant_or_transaction():
    settings = settings_with_quota(burst=2)
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug="pilot-rate-metrics",
        name="Pilot Rate Metrics",
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="integration",
        scopes=["decisions:write", "metrics:read"],
    )
    headers = authorization(issued.plaintext)
    with TestClient(app) as client:
        client.post("/v1/decisions", json=payment("txn-rate-secret"), headers=headers)
        metrics = client.get("/metrics", headers=headers)
    assert "fraudguard_rate_limit_outcomes_total" in metrics.text
    assert str(tenant_id) not in metrics.text
    assert "txn-rate-secret" not in metrics.text
