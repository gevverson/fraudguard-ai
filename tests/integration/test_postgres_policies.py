import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from src.persistence import (
    Database,
    PolicyActivationConflict,
    PolicyRepository,
    TenantPolicyVersionRow,
)
from src.policy import TenantPolicy

pytestmark = pytest.mark.integration

ADMIN_URL = os.getenv("FRAUDGUARD_TEST_POSTGRES_ADMIN_URL")
APP_URL = os.getenv("FRAUDGUARD_TEST_POSTGRES_APP_URL")


def policy(threshold):
    return TenantPolicy(
        high_amount_review_thresholds={"KES": threshold},
        velocity_decline_count_60s=10,
    )


@pytest.fixture(scope="module")
def postgres_policy_database():
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
    database = Database(APP_URL, auto_create_schema=False)
    yield database
    database.dispose()
    admin.dispose()


def test_policy_rls_isolation_and_database_immutability(postgres_policy_database):
    database = postgres_policy_database
    first_tenant = uuid4()
    second_tenant = uuid4()
    database.ensure_tenant(
        first_tenant, slug=f"policy-rls-a-{first_tenant.hex[:8]}", name="Policy A"
    )
    database.ensure_tenant(
        second_tenant, slug=f"policy-rls-b-{second_tenant.hex[:8]}", name="Policy B"
    )
    repository = PolicyRepository(database)
    repository.create_version(
        tenant_id=first_tenant,
        policy_version="first-v1",
        policy=policy("100.00"),
        actor="operator:integration",
    )
    repository.create_version(
        tenant_id=second_tenant,
        policy_version="second-v1",
        policy=policy("200.00"),
        actor="operator:integration",
    )

    with database.engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
            {"tenant": str(first_tenant)},
        )
        visible = connection.execute(
            select(TenantPolicyVersionRow.policy_version).where(
                TenantPolicyVersionRow.policy_version.in_(["first-v1", "second-v1"])
            )
        ).scalars().all()
        assert visible == ["first-v1"]

    with pytest.raises(DBAPIError, match="immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('fraudguard.tenant_id', :tenant, true)"),
                {"tenant": str(first_tenant)},
            )
            connection.execute(
                text(
                    "UPDATE tenant_policy_versions SET document_sha256 = :digest "
                    "WHERE tenant_id = :tenant AND policy_version = 'first-v1'"
                ),
                {"digest": "0" * 64, "tenant": first_tenant},
            )


def test_concurrent_activation_allows_one_generation_winner(
    postgres_policy_database,
):
    database = postgres_policy_database
    tenant_id = uuid4()
    database.ensure_tenant(
        tenant_id,
        slug=f"policy-race-{tenant_id.hex[:8]}",
        name="Policy Race",
    )
    repository = PolicyRepository(database)
    for version, threshold in (("race-a", "100.00"), ("race-b", "200.00")):
        repository.create_version(
            tenant_id=tenant_id,
            policy_version=version,
            policy=policy(threshold),
            actor="operator:integration",
        )

    def activate(version):
        try:
            result = repository.activate(
                tenant_id=tenant_id,
                policy_version=version,
                actor=f"operator:{version}",
                expected_generation=1,
            )
            return result.snapshot.policy_version
        except PolicyActivationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(activate, ("race-a", "race-b")))

    assert sorted(outcomes) in (["conflict", "race-a"], ["conflict", "race-b"])
    active = repository.resolve_active(tenant_id)
    assert active.generation == 2
    assert active.snapshot.policy_version in {"race-a", "race-b"}
