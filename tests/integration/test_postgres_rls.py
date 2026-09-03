import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from src.contracts import DecisionRequest
from src.persistence import (
    Database,
    DecisionLedger,
    request_fingerprint,
    subject_fingerprints,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.getenv("FRAUDGUARD_TEST_POSTGRES_ADMIN_URL")
APP_URL = os.getenv("FRAUDGUARD_TEST_POSTGRES_APP_URL")


def payment(transaction_id):
    return DecisionRequest.model_validate(
        {
            "transaction_id": transaction_id,
            "occurred_at": "2026-09-01T09:30:00Z",
            "amount": 10,
            "currency": "KES",
            "channel": "mobile_money",
            "transaction_type": "transfer",
            "identifiers": {"customer_id": "cus_postgres_subject"},
        }
    )


def complete(ledger, claim, credential_id):
    ledger.complete(
        claim,
        credential_id=credential_id,
        request_id="postgres-integration",
        response_json='{"decision":"approve"}',
        action="approve",
        mode="rules-only-shadow",
        schema_version="1.1",
        feature_version="velocity-v1",
        feature_status="available",
        model_version=None,
        service_version="0.3.0",
        enforced=False,
        rule_hits_json="[]",
    )


@pytest.fixture(scope="module")
def postgres_environment():
    if not ADMIN_URL or not APP_URL:
        pytest.skip("PostgreSQL integration URLs are not configured")

    admin = create_engine(ADMIN_URL)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'fraudguard_test_app'
                    ) THEN
                        CREATE ROLE fraudguard_test_app
                        LOGIN PASSWORD 'fraudguard-test-app-password'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                    END IF;
                END
                $$
                """
            )
        )
        connection.execute(text("GRANT CONNECT ON DATABASE fraudguard TO fraudguard_test_app"))
        connection.execute(text("GRANT USAGE ON SCHEMA public TO fraudguard_test_app"))
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO fraudguard_test_app"
            )
        )
        connection.execute(
            text(
                "REVOKE UPDATE, DELETE ON decision_audit_events "
                "FROM fraudguard_test_app"
            )
        )

    database = Database(APP_URL, auto_create_schema=False)
    yield database
    database.dispose()
    admin.dispose()


def reserve_and_complete(database, ledger, tenant_id, transaction_id):
    credential_id = uuid4()
    value = payment(transaction_id)
    key = b"k" * 32
    claim = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration",
    )
    complete(ledger, claim, credential_id)
    return claim


def test_runtime_role_and_rls_block_cross_tenant_access(postgres_environment):
    database = postgres_environment
    first_tenant = uuid4()
    second_tenant = uuid4()
    database.ensure_tenant(
        first_tenant,
        slug=f"rls-a-{first_tenant.hex[:8]}",
        name="RLS A",
    )
    database.ensure_tenant(
        second_tenant,
        slug=f"rls-b-{second_tenant.hex[:8]}",
        name="RLS B",
    )
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    first = reserve_and_complete(database, ledger, first_tenant, "txn-rls-a")
    second = reserve_and_complete(database, ledger, second_tenant, "txn-rls-b")

    with database.engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
            {"tenant": str(first_tenant)},
        )
        visible = connection.execute(
            text("SELECT decision_id, tenant_id FROM decision_records")
        ).all()
        assert visible == [(first.decision_id, first_tenant)]
        cross_tenant_update = connection.execute(
            text(
                "UPDATE decision_records SET action = 'review' "
                "WHERE decision_id = :decision_id"
            ),
            {"decision_id": second.decision_id},
        )
        assert cross_tenant_update.rowcount == 0

    with database.engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM decision_records")
            ).scalar_one()
            == 0
        )
        role = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        assert role == (False, False)


def test_postgres_audit_events_are_append_only(postgres_environment):
    database = postgres_environment
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"audit-{tenant_id.hex[:8]}",
        name="Audit Tenant",
    )
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    reserve_and_complete(database, ledger, tenant_id, "txn-audit")

    with pytest.raises(DBAPIError):
        with database.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            connection.execute(
                text(
                    "UPDATE decision_audit_events "
                    "SET event_type = 'tampered'"
                )
            )


def test_postgres_completed_decisions_are_immutable(postgres_environment):
    database = postgres_environment
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"immutable-{tenant_id.hex[:8]}",
        name="Immutable Tenant",
    )
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    claim = reserve_and_complete(
        database,
        ledger,
        tenant_id,
        "txn-immutable",
    )

    with pytest.raises(DBAPIError):
        with database.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            connection.execute(
                text(
                    "UPDATE decision_records "
                    "SET response_json = '{\"decision\":\"decline\"}' "
                    "WHERE decision_id = :decision_id"
                ),
                {"decision_id": claim.decision_id},
            )


def test_postgres_terminal_failures_are_immutable(postgres_environment):
    database = postgres_environment
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"failed-{tenant_id.hex[:8]}",
        name="Failed Tenant",
    )
    credential_id = uuid4()
    value = payment("txn-terminal-immutable")
    key = b"k" * 32
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    claim = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration",
    )
    ledger.fail(
        claim,
        credential_id=credential_id,
        request_id="postgres-integration",
        failure_code="decision_evaluation_failed",
        retryable=False,
        response_json='{"detail":{"code":"decision_evaluation_failed"}}',
    )

    with pytest.raises(DBAPIError):
        with database.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            connection.execute(
                text(
                    "UPDATE decision_records SET failure_code = 'tampered' "
                    "WHERE decision_id = :decision_id"
                ),
                {"decision_id": claim.decision_id},
            )


def test_postgres_retryable_failure_can_transition_to_completed(
    postgres_environment,
):
    database = postgres_environment
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"retryable-{tenant_id.hex[:8]}",
        name="Retryable Tenant",
    )
    credential_id = uuid4()
    value = payment("txn-retryable-transition")
    key = b"k" * 32
    ledger = DecisionLedger(database, fingerprint_key_version="v1")
    claim = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration",
    )
    ledger.fail(
        claim,
        credential_id=credential_id,
        request_id="postgres-integration",
        failure_code="velocity_unavailable",
        retryable=True,
    )
    reclaimed = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration-retry",
    )
    complete(ledger, reclaimed, credential_id)


def test_postgres_expired_lease_can_transition_to_completed(
    postgres_environment,
):
    database = postgres_environment
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"expired-{tenant_id.hex[:8]}",
        name="Expired Lease Tenant",
    )
    credential_id = uuid4()
    value = payment("txn-expired-transition")
    key = b"k" * 32
    clock_value = [datetime(2026, 9, 1, tzinfo=UTC)]
    ledger = DecisionLedger(
        database,
        fingerprint_key_version="v1",
        lease_seconds=5,
        clock=lambda: clock_value[0],
    )
    original = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration",
    )
    clock_value[0] += timedelta(seconds=5)
    reclaimed = ledger.reserve(
        tenant_id=tenant_id,
        credential_id=credential_id,
        payment=value,
        fingerprint=request_fingerprint(value, key),
        subject_hashes=subject_fingerprints(value, key),
        request_id="postgres-integration-retry",
    )
    assert reclaimed.decision_id == original.decision_id
    assert reclaimed.lease_token != original.lease_token
    complete(ledger, reclaimed, credential_id)


def test_postgres_readiness_requires_runtime_table_privileges(
    postgres_environment,
):
    database = postgres_environment
    assert database.ready() is True
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as connection:
            connection.execute(
                text("REVOKE UPDATE ON decision_records FROM fraudguard_test_app")
            )
        assert database.ready() is False
    finally:
        with admin.begin() as connection:
            connection.execute(
                text("GRANT UPDATE ON decision_records TO fraudguard_test_app")
            )
        admin.dispose()
    assert database.ready() is True
