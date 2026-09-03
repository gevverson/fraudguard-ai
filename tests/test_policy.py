from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.persistence import (
    Database,
    PolicyActivationConflict,
    PolicyRepository,
    PolicyUnavailable,
    TenantPolicyActivationEventRow,
    TenantPolicyVersionRow,
)
from src.policy import (
    DEFAULT_POLICY_VERSION,
    TenantPolicy,
    policy_document,
    policy_sha256,
    validate_policy_version,
)


@pytest.fixture
def policy_environment():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    database.ensure_tenant(tenant_id, slug="policy-a", name="Policy A")
    yield database, PolicyRepository(database), tenant_id
    database.dispose()


def custom_policy(*, kes_threshold="100.00", velocity_count=5):
    return TenantPolicy(
        high_amount_review_thresholds={"KES": kes_threshold},
        velocity_decline_count_60s=velocity_count,
    )


def test_policy_validation_is_deterministic_and_deeply_immutable():
    first = custom_policy(kes_threshold="100.0")
    second = custom_policy(kes_threshold=Decimal("100.00"))
    assert policy_document(first) == policy_document(second)
    assert policy_sha256(first) == policy_sha256(second)
    with pytest.raises(TypeError, match="immutable"):
        first.high_amount_review_thresholds["USD"] = Decimal("1.00")

    for invalid in ("bad version", "-starts-wrong", "x" * 65):
        with pytest.raises(ValueError):
            validate_policy_version(invalid)
    with pytest.raises(ValueError):
        custom_policy(kes_threshold="0")
    with pytest.raises(ValueError):
        TenantPolicy(high_amount_review_thresholds={"kes": "100"})


def test_new_tenant_starts_on_audited_default_policy(policy_environment):
    database, repository, tenant_id = policy_environment
    active = repository.resolve_active(tenant_id)
    assert active.snapshot.policy_version == DEFAULT_POLICY_VERSION
    assert active.generation == 1
    with database.sessions() as session:
        event = session.scalar(
            select(TenantPolicyActivationEventRow).where(
                TenantPolicyActivationEventRow.tenant_id == tenant_id
            )
        )
        assert event.policy_version == DEFAULT_POLICY_VERSION
        assert event.previous_policy_version is None


def test_versions_are_create_only_and_tenant_scoped(policy_environment):
    _database, repository, tenant_id = policy_environment
    snapshot = repository.create_version(
        tenant_id=tenant_id,
        policy_version="pilot-kes-v1",
        policy=custom_policy(),
        actor="operator:gevverson",
    )
    assert snapshot.policy_version == "pilot-kes-v1"
    assert repository.resolve_active(tenant_id).snapshot.policy_version == "rules-v2"
    with pytest.raises(ValueError, match="already exists"):
        repository.create_version(
            tenant_id=tenant_id,
            policy_version="pilot-kes-v1",
            policy=custom_policy(kes_threshold="200"),
            actor="operator:gevverson",
        )
    with pytest.raises(PolicyUnavailable):
        repository.get_version(uuid4(), "pilot-kes-v1")


def test_activation_uses_generation_lock_and_rollback_is_audited(
    policy_environment,
):
    database, repository, tenant_id = policy_environment
    repository.create_version(
        tenant_id=tenant_id,
        policy_version="pilot-kes-v1",
        policy=custom_policy(),
        actor="operator:gevverson",
    )
    activated = repository.activate(
        tenant_id=tenant_id,
        policy_version="pilot-kes-v1",
        actor="operator:gevverson",
        expected_generation=1,
    )
    assert activated.snapshot.policy_version == "pilot-kes-v1"
    assert activated.generation == 2

    with pytest.raises(PolicyActivationConflict, match="changed"):
        repository.activate(
            tenant_id=tenant_id,
            policy_version=DEFAULT_POLICY_VERSION,
            actor="operator:stale",
            expected_generation=1,
        )

    rolled_back = repository.rollback(
        tenant_id=tenant_id,
        actor="operator:gevverson",
        expected_generation=2,
    )
    assert rolled_back.snapshot.policy_version == DEFAULT_POLICY_VERSION
    assert rolled_back.generation == 3
    with database.sessions() as session:
        assert session.scalar(
            select(func.count())
            .select_from(TenantPolicyActivationEventRow)
            .where(TenantPolicyActivationEventRow.tenant_id == tenant_id)
        ) == 3


def test_stored_policy_integrity_failure_is_detected(policy_environment):
    database, repository, tenant_id = policy_environment
    with database.sessions.begin() as session:
        row = session.get(
            TenantPolicyVersionRow, (tenant_id, DEFAULT_POLICY_VERSION)
        )
        row.policy_document = policy_document(custom_policy())
    with pytest.raises(PolicyUnavailable, match="integrity"):
        repository.get_version(tenant_id, DEFAULT_POLICY_VERSION)
