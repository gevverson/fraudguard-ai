"""Add immutable tenant policies and atomic activation provenance.

Revision ID: 20260903_0002
Revises: 20260901_0001
Create Date: 2026-09-03
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Sequence
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision: str = "20260903_0002"
down_revision: str | None = "20260901_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_VERSION = "rules-v2"
DEFAULT_DOCUMENT = {
    "policy_schema_version": "1",
    "high_amount_review_thresholds": {"USD": "10000.00"},
    "velocity_decline_count_60s": 10,
}
DEFAULT_JSON = json.dumps(
    DEFAULT_DOCUMENT,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
DEFAULT_SHA256 = hashlib.sha256(DEFAULT_JSON.encode()).hexdigest()


def upgrade() -> None:
    op.create_table(
        "tenant_policy_versions",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_schema_version", sa.String(length=16), nullable=False),
        sa.Column("policy_document", sa.JSON(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "policy_version"),
    )
    op.create_table(
        "tenant_policy_activations",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("active_policy_version", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("activated_by", sa.String(length=128), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation >= 1", name="ck_policy_activation_generation"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "active_policy_version"],
            ["tenant_policy_versions.tenant_id", "tenant_policy_versions.policy_version"],
            name="fk_policy_activation_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_table(
        "tenant_policy_activation_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("previous_policy_version", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("activated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_policy_event_generation"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_version"],
            ["tenant_policy_versions.tenant_id", "tenant_policy_versions.policy_version"],
            name="fk_policy_event_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "tenant_id", "generation", name="uq_policy_event_tenant_generation"
        ),
    )
    op.create_index(
        "ix_policy_event_tenant_created",
        "tenant_policy_activation_events",
        ["tenant_id", "created_at"],
        unique=False,
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS decision_records_transition_guard "
            "ON decision_records"
        )
    tenant_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants"))]
    now = datetime.now(UTC)
    versions = sa.table(
        "tenant_policy_versions",
        sa.column("tenant_id", sa.Uuid()),
        sa.column("policy_version", sa.String()),
        sa.column("policy_schema_version", sa.String()),
        sa.column("policy_document", sa.JSON()),
        sa.column("document_sha256", sa.String()),
        sa.column("created_by", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    activations = sa.table(
        "tenant_policy_activations",
        sa.column("tenant_id", sa.Uuid()),
        sa.column("active_policy_version", sa.String()),
        sa.column("generation", sa.Integer()),
        sa.column("activated_by", sa.String()),
        sa.column("activated_at", sa.DateTime(timezone=True)),
    )
    events = sa.table(
        "tenant_policy_activation_events",
        sa.column("event_id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("policy_version", sa.String()),
        sa.column("previous_policy_version", sa.String()),
        sa.column("generation", sa.Integer()),
        sa.column("activated_by", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    if tenant_ids:
        bind.execute(
            versions.insert(),
            [
                {
                    "tenant_id": tenant_id,
                    "policy_version": DEFAULT_VERSION,
                    "policy_schema_version": "1",
                    "policy_document": DEFAULT_DOCUMENT,
                    "document_sha256": DEFAULT_SHA256,
                    "created_by": "system:migration",
                    "created_at": now,
                }
                for tenant_id in tenant_ids
            ],
        )
        bind.execute(
            activations.insert(),
            [
                {
                    "tenant_id": tenant_id,
                    "active_policy_version": DEFAULT_VERSION,
                    "generation": 1,
                    "activated_by": "system:migration",
                    "activated_at": now,
                }
                for tenant_id in tenant_ids
            ],
        )
        bind.execute(
            events.insert(),
            [
                {
                    "event_id": uuid4(),
                    "tenant_id": tenant_id,
                    "policy_version": DEFAULT_VERSION,
                    "previous_policy_version": None,
                    "generation": 1,
                    "activated_by": "system:migration",
                    "created_at": now,
                }
                for tenant_id in tenant_ids
            ],
        )

    for tenant_id in tenant_ids:
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text(
                    "SELECT set_config('fraudguard.tenant_id', :tenant_id, true)"
                ),
                {"tenant_id": str(tenant_id)},
            )
        bind.execute(
            sa.text(
                "UPDATE decision_records SET policy_version = :version "
                "WHERE tenant_id = :tenant_id AND policy_version IS NULL"
            ),
            {"tenant_id": tenant_id, "version": DEFAULT_VERSION},
        )
    op.alter_column(
        "decision_records",
        "policy_version",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_decision_policy_version",
        "decision_records",
        "tenant_policy_versions",
        ["tenant_id", "policy_version"],
        ["tenant_id", "policy_version"],
        ondelete="RESTRICT",
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS decision_records_policy_immutable "
            "ON decision_records"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS fraudguard_prevent_decision_policy_mutation()"
        )
        tenant_expression = (
            "tenant_id = NULLIF("
            "current_setting('fraudguard.tenant_id', true), '')::uuid"
        )
        for table in (
            "tenant_policy_versions",
            "tenant_policy_activations",
            "tenant_policy_activation_events",
        ):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_tenant_isolation ON {table} "
                f"USING ({tenant_expression}) WITH CHECK ({tenant_expression})"
            )

        op.execute(
            """
            CREATE FUNCTION fraudguard_prevent_policy_version_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'tenant policy versions are immutable';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER tenant_policy_versions_immutable
            BEFORE UPDATE OR DELETE ON tenant_policy_versions
            FOR EACH ROW EXECUTE FUNCTION fraudguard_prevent_policy_version_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER tenant_policy_activation_events_append_only
            BEFORE UPDATE OR DELETE ON tenant_policy_activation_events
            FOR EACH ROW EXECUTE FUNCTION fraudguard_prevent_audit_mutation()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION fraudguard_enforce_policy_activation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                    OR NEW.active_policy_version IS NOT DISTINCT FROM OLD.active_policy_version
                    OR NEW.generation <> OLD.generation + 1
                    OR NEW.activated_at < OLD.activated_at
                THEN
                    RAISE EXCEPTION 'invalid tenant policy activation transition';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER tenant_policy_activation_guard
            BEFORE UPDATE ON tenant_policy_activations
            FOR EACH ROW EXECUTE FUNCTION fraudguard_enforce_policy_activation()
            """
        )
        op.execute(
            """
            CREATE FUNCTION fraudguard_prevent_decision_policy_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.policy_version IS DISTINCT FROM OLD.policy_version THEN
                    RAISE EXCEPTION 'decision policy version is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER decision_records_policy_immutable
            BEFORE UPDATE ON decision_records
            FOR EACH ROW
            EXECUTE FUNCTION fraudguard_prevent_decision_policy_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER decision_records_transition_guard
            BEFORE UPDATE ON decision_records
            FOR EACH ROW
            EXECUTE FUNCTION fraudguard_enforce_decision_transition()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS decision_records_policy_immutable "
            "ON decision_records"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS fraudguard_prevent_decision_policy_mutation()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS tenant_policy_activation_guard "
            "ON tenant_policy_activations"
        )
        op.execute("DROP FUNCTION IF EXISTS fraudguard_enforce_policy_activation()")
        op.execute(
            "DROP TRIGGER IF EXISTS tenant_policy_activation_events_append_only "
            "ON tenant_policy_activation_events"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS tenant_policy_versions_immutable "
            "ON tenant_policy_versions"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS fraudguard_prevent_policy_version_mutation()"
        )

    op.drop_constraint(
        "fk_decision_policy_version", "decision_records", type_="foreignkey"
    )
    op.alter_column(
        "decision_records",
        "policy_version",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.drop_index(
        "ix_policy_event_tenant_created",
        table_name="tenant_policy_activation_events",
    )
    op.drop_table("tenant_policy_activation_events")
    op.drop_table("tenant_policy_activations")
    op.drop_table("tenant_policy_versions")
