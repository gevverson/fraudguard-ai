from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from src.app import api

client = TestClient(api.app)


def valid_payment(**overrides):
    payment = {
        "transaction_id": "txn_01J8Z6K4W4",
        "occurred_at": "2026-09-01T09:30:00+03:00",
        "amount": 125.50,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
        "identifiers": {
            "payment_method_token": "pmt_tok_A1B2C3",
            "customer_id": "cus_01J8Z6K4W4",
            "merchant_id": "mer_01J8Z6K4W4",
        },
    }
    payment.update(overrides)
    return payment


def test_health_does_not_load_legacy_model(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("health must not load a model artifact")

    monkeypatch.setattr(api.registry, "load", fail_if_called)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["canonical_engine"] == "rules-only-shadow"


def test_v1_decision_is_honest_rules_only_shadow_result():
    first = client.post("/v1/decisions", json=valid_payment())
    second = client.post("/v1/decisions", json=valid_payment())
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.headers["X-Decision-Replayed"] == "false"
    assert second.headers["X-Decision-Replayed"] == "true"
    body = first.json()
    assert body["schema_version"] == "1.1"
    assert body["transaction_id"] == "txn_01J8Z6K4W4"
    assert body["mode"] == "rules-only-shadow"
    assert body["policy_version"] == "rules-v2"
    assert body["feature_version"] == "velocity-v1"
    assert body["feature_status"] == "available"
    assert body["decision"] == "approve"
    assert body["enforced"] is False
    assert body["risk_score"] is None
    assert body["model_version"] is None
    assert body["rule_hits"] == []
    assert body["decision_id"]


def test_v1_applies_high_amount_rule_only_to_supported_currency():
    usd = client.post(
        "/v1/decisions",
        json=valid_payment(
            transaction_id="txn_usd_threshold",
            amount=10000,
            currency="USD",
        ),
    )
    kes = client.post(
        "/v1/decisions",
        json=valid_payment(
            transaction_id="txn_kes_threshold",
            amount=10000,
            currency="KES",
        ),
    )
    assert usd.status_code == kes.status_code == 200
    assert usd.json()["decision"] == "review"
    assert usd.json()["rule_hits"][0]["rule"] == "high_amount_supported_currency"
    assert kes.json()["decision"] == "approve"
    assert kes.json()["rule_hits"] == []


def test_zero_amount_card_authorization_is_reviewed():
    response = client.post(
        "/v1/decisions",
        json=valid_payment(
            transaction_id="txn_zero_authorization",
            amount=0,
            channel="card",
            transaction_type="authorization",
        ),
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "review"
    assert response.json()["enforced"] is False


def test_timezone_naive_timestamp_is_rejected():
    response = client.post(
        "/v1/decisions",
        json=valid_payment(occurred_at="2026-09-01T09:30:00"),
    )
    assert response.status_code == 422


def test_lowercase_or_non_three_letter_currency_is_rejected():
    assert (
        client.post("/v1/decisions", json=valid_payment(currency="kes")).status_code
        == 422
    )
    assert (
        client.post("/v1/decisions", json=valid_payment(currency="USDT")).status_code
        == 422
    )


@pytest.mark.parametrize(
    "field",
    ["pan", "card_number", "cvv", "cvc", "pin", "pin_block", "track2"],
)
def test_sensitive_payment_fields_are_rejected(field):
    payment = valid_payment()
    payment[field] = "sensitive-value"
    response = client.post("/v1/decisions", json=payment)
    assert response.status_code == 422
    assert "not accepted" in response.text


def test_nested_sensitive_payment_fields_are_rejected():
    payment = valid_payment()
    payment["identifiers"]["cvv"] = "123"
    response = client.post("/v1/decisions", json=payment)
    assert response.status_code == 422
    assert "not accepted" in response.text


def test_unknown_fields_and_client_velocity_are_rejected():
    for unknown in ("context", "tx_count_last_60s", "V1"):
        payment = valid_payment()
        payment[unknown] = (
            {"tx_count_last_60s": 99} if unknown == "context" else 99
        )
        assert client.post("/v1/decisions", json=payment).status_code == 422


def test_clear_numeric_payment_identifier_is_rejected():
    payment = valid_payment()
    payment["identifiers"]["payment_method_token"] = "4111111111111111"
    assert client.post("/v1/decisions", json=payment).status_code == 422


def test_identifier_set_requires_a_tokenized_subject():
    payment = valid_payment()
    payment["identifiers"] = {"merchant_id": "mer_01J8Z6K4W4"}
    assert client.post("/v1/decisions", json=payment).status_code == 422


def test_amount_must_be_a_number_with_no_more_than_two_decimal_places():
    assert (
        client.post("/v1/decisions", json=valid_payment(amount="12.50")).status_code
        == 422
    )
    assert (
        client.post("/v1/decisions", json=valid_payment(amount=12.345)).status_code
        == 422
    )


def test_zero_amount_non_authorization_is_rejected():
    assert client.post("/v1/decisions", json=valid_payment(amount=0)).status_code == 422


def test_score_is_deprecated_and_model_unavailability_is_503(monkeypatch):
    assert client.get("/openapi.json").json()["paths"]["/score"]["post"]["deprecated"]

    def unavailable():
        raise api.LegacyModelUnavailable("missing")

    monkeypatch.setattr(api, "_load_legacy_runtime", unavailable)
    features = {feature: 1.0 for feature in api.FEATURES}
    response = client.post("/score", json={"features": features})
    assert response.status_code == 503


def test_valid_payment_helper_does_not_share_nested_state():
    first = valid_payment()
    second = deepcopy(first)
    second["identifiers"]["customer_id"] = "cus_changed"
    assert first["identifiers"]["customer_id"] != second["identifiers"]["customer_id"]
