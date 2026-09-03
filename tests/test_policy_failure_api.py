import base64

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.app.api import create_app
from src.config import Settings
from src.persistence import DecisionRecordRow, PolicyUnavailable
from src.security import provision_api_key


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


class UnavailablePolicies:
    def get_version(self, _tenant_id, _policy_version):
        raise PolicyUnavailable("offline")


def test_policy_outage_is_retryable_and_returns_specific_safe_error():
    settings = Settings(
        environment="test",
        auth_mode="required",
        api_key_peppers={"v1": encoded_secret()},
        data_pepper=encoded_secret(b"d"),
        velocity_warmup_seconds=0,
    )
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug="policy-outage", name="Policy Outage"
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="policy-outage",
        scopes=["decisions:write"],
    )
    app.state.runtime.service._policies = UnavailablePolicies()
    request = {
        "transaction_id": "txn-policy-outage",
        "occurred_at": "2026-09-03T10:00:00+03:00",
        "amount": 25,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
        "identifiers": {"customer_id": "cus_policy_outage"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/decisions",
            json=request,
            headers={"Authorization": f"Bearer {issued.plaintext}"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "policy_unavailable"
        assert response.headers["Retry-After"] == "1"
        with app.state.runtime.database.sessions() as session:
            row = session.scalar(select(DecisionRecordRow))
            assert row.state == "failed"
            assert row.retryable is True
            assert row.failure_code == "policy_unavailable"
