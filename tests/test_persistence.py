import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.config import Settings
from src.contracts import DecisionRequest
from src.persistence import (
    ApiCredentialRow,
    Base,
    CredentialRepository,
    Database,
    DecisionAuditRow,
    DecisionClaim,
    DecisionFailed,
    DecisionInProgress,
    DecisionLedger,
    DecisionRecordRow,
    IdempotencyConflict,
    LedgerUnavailable,
    OutboxRow,
    StoredDecision,
    canonical_request_bytes,
    request_fingerprint,
    subject_fingerprints,
)
from src.security import provision_api_key


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def payment(**overrides):
    document = {
        "transaction_id": "txn-ledger-1",
        "occurred_at": "2026-09-01T09:30:00+03:00",
        "amount": 125.5,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
        "identifiers": {
            "payment_method_token": "pmt_tok_secret",
            "customer_id": "cus_tok_secret",
        },
    }
    document.update(overrides)
    return DecisionRequest.model_validate(document)


@pytest.fixture
def ledger_environment():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    database.ensure_tenant(tenant_id, slug="tenant-a", name="Tenant A")
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    yield database, ledger, tenant_id
    database.dispose()


def reserve(ledger, tenant_id, payment_value, *, credential_id=None):
    key = b"f" * 32
    return ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id or uuid4(),
        payment=payment_value,
        fingerprint=request_fingerprint(payment_value, key),
        subject_hashes=subject_fingerprints(payment_value, key),
        request_id="request-1",
    )


def complete(ledger, claim, *, credential_id=None):
    body = (
        '{"schema_version":"1.1","decision_id":"'
        + str(claim.decision_id)
        + '","decision":"approve"}'
    )
    return ledger.complete(
        claim,
        credential_id=credential_id or uuid4(),
        request_id="request-1",
        response_json=body,
        action="approve",
        mode="rules-only-shadow",
        policy_version="rules-v2",
        schema_version="1.1",
        feature_version="velocity-v1",
        feature_status="available",
        model_version=None,
        service_version="0.3.0",
        enforced=False,
        rule_hits_json="[]",
    )


def test_canonical_request_normalizes_decimal_and_timezone():
    first = payment(amount=1, occurred_at="2026-09-01T09:30:00+03:00")
    second = payment(amount=1.0, occurred_at="2026-09-01T06:30:00Z")
    assert canonical_request_bytes(first) == canonical_request_bytes(second)
    assert request_fingerprint(first, b"k" * 32) == request_fingerprint(
        second, b"k" * 32
    )


def test_meaningful_request_change_changes_fingerprint():
    assert request_fingerprint(payment(), b"k" * 32) != request_fingerprint(
        payment(amount=126), b"k" * 32
    )


def test_first_completion_and_replay_use_exact_stored_response(ledger_environment):
    database, ledger, tenant_id = ledger_environment
    value = payment()
    claim = reserve(ledger, tenant_id, value)
    assert isinstance(claim, DecisionClaim)
    completed = complete(ledger, claim)
    replay = reserve(ledger, tenant_id, value)
    assert isinstance(completed, StoredDecision)
    assert isinstance(replay, StoredDecision)
    assert replay.replayed is True
    assert replay.response_json == completed.response_json
    assert replay.decision_id == completed.decision_id

    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 1
        assert session.scalar(select(func.count()).select_from(DecisionAuditRow)) == 2


def test_payload_mismatch_under_same_tenant_transaction_conflicts(
    ledger_environment,
):
    _database, ledger, tenant_id = ledger_environment
    claim = reserve(ledger, tenant_id, payment())
    complete(ledger, claim)
    with pytest.raises(IdempotencyConflict):
        reserve(ledger, tenant_id, payment(amount=999))


def test_active_reservation_returns_in_progress(ledger_environment):
    _database, ledger, tenant_id = ledger_environment
    reserve(ledger, tenant_id, payment())
    with pytest.raises(DecisionInProgress):
        reserve(ledger, tenant_id, payment())


def test_retryable_failure_can_be_reclaimed(ledger_environment):
    _database, ledger, tenant_id = ledger_environment
    credential_id = uuid4()
    claim = reserve(
        ledger,
        tenant_id,
        payment(),
        credential_id=credential_id,
    )
    ledger.fail(
        claim,
        credential_id=credential_id,
        request_id="request-1",
        failure_code="velocity_unavailable",
        retryable=True,
    )
    reclaimed = reserve(
        ledger,
        tenant_id,
        payment(),
        credential_id=credential_id,
    )
    assert isinstance(reclaimed, DecisionClaim)
    assert reclaimed.decision_id == claim.decision_id
    assert reclaimed.lease_token != claim.lease_token


def test_terminal_failure_replays_the_exact_stored_error(ledger_environment):
    database, ledger, tenant_id = ledger_environment
    value = payment()
    credential_id = uuid4()
    claim = reserve(
        ledger,
        tenant_id,
        value,
        credential_id=credential_id,
    )
    response_json = (
        '{"decision_id":"'
        + str(claim.decision_id)
        + '","detail":{"code":"decision_evaluation_failed"}}'
    )
    ledger.fail(
        claim,
        credential_id=credential_id,
        request_id="request-1",
        failure_code="decision_evaluation_failed",
        retryable=False,
        response_json=response_json,
    )

    with pytest.raises(DecisionFailed) as captured:
        reserve(
            ledger,
            tenant_id,
            value,
            credential_id=credential_id,
        )
    assert captured.value.decision_id == claim.decision_id
    assert captured.value.response_json == response_json
    assert captured.value.replayed is True
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 0
        assert session.scalar(select(func.count()).select_from(DecisionAuditRow)) == 2


def test_replay_uses_the_stored_fingerprint_key_version():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    database.ensure_tenant(tenant_id, slug="tenant-a", name="Tenant A")
    value = payment()
    old_key = b"o" * 32
    new_key = b"n" * 32
    original = DecisionLedger(
        database,
        fingerprint_key_version="v1",
        fingerprint_keys={"v1": old_key},
    )
    claim = original.reserve(
        tenant_id=tenant_id,
        credential_id=uuid4(),
        payment=value,
        fingerprint=request_fingerprint(value, old_key),
        subject_hashes=subject_fingerprints(value, old_key),
        request_id="request-v1",
    )
    completed = complete(original, claim)

    rotated = DecisionLedger(
        database,
        fingerprint_key_version="v2",
        fingerprint_keys={"v1": old_key, "v2": new_key},
    )
    replay = rotated.reserve(
        tenant_id=tenant_id,
        credential_id=uuid4(),
        payment=value,
        fingerprint=request_fingerprint(value, new_key),
        subject_hashes=subject_fingerprints(value, new_key),
        request_id="request-v2",
    )
    assert isinstance(replay, StoredDecision)
    assert replay.response_json == completed.response_json
    assert replay.replayed is True

    missing_old_key = DecisionLedger(
        database,
        fingerprint_key_version="v2",
        fingerprint_keys={"v2": new_key},
    )
    with pytest.raises(LedgerUnavailable, match="key version"):
        missing_old_key.reserve(
            tenant_id=tenant_id,
            credential_id=uuid4(),
            payment=value,
            fingerprint=request_fingerprint(value, new_key),
            subject_hashes=subject_fingerprints(value, new_key),
            request_id="request-v2-no-old-key",
        )
    database.dispose()


def test_expired_processing_lease_is_reclaimed_once():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    database.ensure_tenant(tenant_id, slug="tenant-a", name="Tenant A")
    clock_value = [now]
    ledger = DecisionLedger(
        database,
        fingerprint_key_version="v1",
        lease_seconds=5,
        clock=lambda: clock_value[0],
    )
    first = reserve(ledger, tenant_id, payment())
    clock_value[0] = now + timedelta(seconds=5)
    second = reserve(ledger, tenant_id, payment())
    assert second.decision_id == first.decision_id
    assert second.lease_token != first.lease_token
    database.dispose()


def test_transaction_identifier_is_unique_only_within_tenant(ledger_environment):
    database, ledger, first_tenant = ledger_environment
    second_tenant = uuid4()
    database.ensure_tenant(second_tenant, slug="tenant-b", name="Tenant B")
    first = reserve(ledger, first_tenant, payment())
    second = reserve(ledger, second_tenant, payment())
    assert first.decision_id != second.decision_id


def test_subject_tokens_are_replaced_with_hmac_fingerprints(ledger_environment):
    database, ledger, tenant_id = ledger_environment
    reserve(ledger, tenant_id, payment())
    with database.sessions() as session:
        row = session.scalar(select(DecisionRecordRow))
        serialized = json.dumps(row.subject_fingerprints)
    assert "pmt_tok_secret" not in serialized
    assert "cus_tok_secret" not in serialized
    assert set(row.subject_fingerprints) == {
        "customer_id",
        "payment_method_token",
    }


def test_sqlite_schema_is_ready_and_foreign_keys_are_enabled(ledger_environment):
    database, _ledger, _tenant_id = ledger_environment
    assert database.ready() is True
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_migrated_readiness_rejects_a_schema_without_an_alembic_revision():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=False)
    Base.metadata.create_all(database.engine)
    assert database.ready() is False
    assert database.ready(require_runtime_privileges=False) is False
    database.dispose()


def test_sqlalchemy_credential_repository_round_trip():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    repository = CredentialRepository(database)
    tenant_id = repository.create_tenant(slug="pilot", name="Pilot")
    settings = Settings(
        environment="test",
        auth_mode="required",
        api_key_peppers={"v1": encoded_secret()},
        data_pepper=encoded_secret(b"d"),
    )
    issued = provision_api_key(
        repository,
        settings=settings,
        tenant_id=tenant_id,
        name="integration",
        scopes=["decisions:write"],
    )
    record = repository.get_by_key_id(issued.credential.key_id)
    assert record is not None
    assert record.tenant_id == tenant_id
    assert record.secret_digest == issued.credential.secret_digest
    assert issued.plaintext not in repr(record)
    assert repository.revoke(record.credential_id) is True
    assert repository.get_by_key_id(record.key_id).status == "revoked"
    assert repository.revoke(uuid4()) is False
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ApiCredentialRow)) == 1
    database.dispose()
