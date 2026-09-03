from uuid import uuid4

import pytest

from src.persistence import (
    Database,
    PolicyActivationConflict,
    PolicyRepository,
    PolicyUnavailable,
)
from src.policy import TenantPolicy, validate_policy_version


def test_policy_rejects_every_unsafe_threshold_shape():
    invalid_thresholds = (
        [],
        {"KES": True},
        {"KES": object()},
        {"KES": "NaN"},
        {"KES": "1.001"},
        {"KES": "10000000000000000.00"},
    )
    for thresholds in invalid_thresholds:
        with pytest.raises(ValueError):
            TenantPolicy(high_amount_review_thresholds=thresholds)
    assert validate_policy_version("  safe-v1  ") == "safe-v1"


def test_policy_operator_edge_cases_are_explicit():
    database = Database("sqlite+pysqlite:///:memory:", auto_create_schema=True)
    tenant_id = uuid4()
    database.ensure_tenant(tenant_id, slug="policy-edges", name="Policy Edges")
    repository = PolicyRepository(database)

    assert [item.policy_version for item in repository.list_versions(tenant_id)] == [
        "rules-v2"
    ]
    unchanged = repository.activate(
        tenant_id=tenant_id,
        policy_version="rules-v2",
        actor="operator:gevverson",
        expected_generation=1,
    )
    assert unchanged.generation == 1
    with pytest.raises(PolicyUnavailable, match="target"):
        repository.activate(
            tenant_id=tenant_id,
            policy_version="missing-v1",
            actor="operator:gevverson",
            expected_generation=1,
        )
    with pytest.raises(PolicyActivationConflict, match="no previous"):
        repository.rollback(
            tenant_id=tenant_id,
            actor="operator:gevverson",
            expected_generation=1,
        )
    with pytest.raises(ValueError, match="actor"):
        repository.create_version(
            tenant_id=tenant_id,
            policy_version="invalid-actor-v1",
            policy=TenantPolicy(),
            actor="",
        )
    with pytest.raises(PolicyUnavailable, match="tenant"):
        repository.create_version(
            tenant_id=uuid4(),
            policy_version="missing-tenant-v1",
            policy=TenantPolicy(),
            actor="operator:gevverson",
        )
    database.dispose()
