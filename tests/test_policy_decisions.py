import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from src.app.api import create_app
from src.config import Settings
from src.contracts import DecisionRequest
from src.persistence import (
    Database,
    DecisionLedger,
    PolicyRepository,
    request_fingerprint,
    subject_fingerprints,
)
from src.policy import TenantPolicy
from src.security import provision_api_key


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def payment(transaction_id="txn-policy-pin", amount=150):
    return DecisionRequest.model_validate(
        {
            "transaction_id": transaction_id,
            "occurred_at": "2026-09-03T10:00:00+03:00",
            "amount": amount,
            "currency": "KES",
            "channel": "mobile_money",
            "transaction_type": "transfer",
            "identifiers": {"customer_id": "cus_policy_subject"},
        }
    )


def api_payment(transaction_id="txn-policy-pin", amount=150):
    document = payment(transaction_id, amount).model_dump(mode="json")
    document["amount"] = float(amount)
    return document


def policy(threshold):
    return TenantPolicy(
        high_amount_review_thresholds={"KES": threshold},
        velocity_decline_count_60s=10,
    )


def test_retry_reclaims_the_original_policy_after_activation_changes():
    now = datetime(2026, 9, 3, tzinfo=UTC)
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    credential_id = uuid4()
    database.ensure_tenant(tenant_id, slug="pin-policy", name="Pin Policy")
    policies = PolicyRepository(database)
    policies.create_version(
        tenant_id=tenant_id,
        policy_version="kes-v1",
        policy=policy("100.00"),
        actor="operator:gevverson",
    )
    policies.create_version(
        tenant_id=tenant_id,
        policy_version="kes-v2",
        policy=policy("1000.00"),
        actor="operator:gevverson",
    )
    policies.activate(
        tenant_id=tenant_id,
        policy_version="kes-v1",
        actor="operator:gevverson",
        expected_generation=1,
    )
    key = b"f" * 32
    value = payment()
    clock = [now]
    ledger = DecisionLedger(
        database,
        fingerprint_key_version="v1",
        lease_seconds=5,
        clock=lambda: clock[0],
    )
    first = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="request-first",
    )
    assert first.policy_version == "kes-v1"

    policies.activate(
        tenant_id=tenant_id,
        policy_version="kes-v2",
        actor="operator:gevverson",
        expected_generation=2,
    )
    clock[0] += timedelta(seconds=5)
    reclaimed = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="request-retry",
    )
    assert reclaimed.decision_id == first.decision_id
    assert reclaimed.policy_version == "kes-v1"
    database.dispose()


def test_api_uses_active_policy_and_replays_exact_old_policy_response():
    settings = Settings(
        environment="test",
        auth_mode="required",
        api_key_peppers={"v1": encoded_secret()},
        data_pepper=encoded_secret(b"d"),
        velocity_warmup_seconds=0,
    )
    app = create_app(settings)
    tenant_id = app.state.runtime.credentials.create_tenant(
        slug="api-policy", name="API Policy"
    )
    issued = provision_api_key(
        app.state.runtime.credentials,
        settings=settings,
        tenant_id=tenant_id,
        name="policy-test",
        scopes=["decisions:write"],
    )
    policies = app.state.runtime.policies
    policies.create_version(
        tenant_id=tenant_id,
        policy_version="kes-review-v1",
        policy=policy("100.00"),
        actor="operator:gevverson",
    )
    policies.create_version(
        tenant_id=tenant_id,
        policy_version="kes-review-v2",
        policy=policy("1000.00"),
        actor="operator:gevverson",
    )
    policies.activate(
        tenant_id=tenant_id,
        policy_version="kes-review-v1",
        actor="operator:gevverson",
        expected_generation=1,
    )
    headers = {"Authorization": f"Bearer {issued.plaintext}"}

    with TestClient(app) as client:
        first = client.post(
            "/v1/decisions",
            json=api_payment(),
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["decision"] == "review"
        assert first.json()["policy_version"] == "kes-review-v1"

        policies.activate(
            tenant_id=tenant_id,
            policy_version="kes-review-v2",
            actor="operator:gevverson",
            expected_generation=2,
        )
        replay = client.post(
            "/v1/decisions",
            json=api_payment(),
            headers=headers,
        )
        current = client.post(
            "/v1/decisions",
            json=api_payment("txn-policy-current"),
            headers=headers,
        )

    assert replay.content == first.content
    assert replay.headers["X-Decision-Replayed"] == "true"
    assert current.status_code == 200
    assert current.json()["decision"] == "approve"
    assert current.json()["policy_version"] == "kes-review-v2"
