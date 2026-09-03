"""Create the tenant-aware shadow-pilot ledger.

Revision ID: 20260901_0001
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260901_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 1 AND 3650",
            name="ck_tenant_retention",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "api_credentials",
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("namespace", sa.String(length=8), nullable=False),
        sa.Column("secret_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("pepper_version", sa.String(length=32), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "namespace IN ('live', 'test')",
            name="ck_credential_namespace",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_credential_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("credential_id"),
        sa.UniqueConstraint("key_id"),
    )
    op.create_index(
        op.f("ix_api_credentials_tenant_id"),
        "api_credentials",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "decision_records",
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "fingerprint_key_version",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "retryable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("subject_fingerprints", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=True),
        sa.Column("feature_version", sa.String(length=64), nullable=True),
        sa.Column("feature_status", sa.String(length=16), nullable=True),
        sa.Column("model_version", sa.String(length=128), nullable=True),
        sa.Column("service_version", sa.String(length=32), nullable=True),
        sa.Column("enforced", sa.Boolean(), nullable=True),
        sa.Column("rule_hits_json", sa.Text(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retention_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "state IN ('processing', 'completed', 'failed')",
            name="ck_decision_state",
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('approve', 'review', 'decline')",
            name="ck_decision_action",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "decision_id",
            name="uq_decision_tenant_decision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "transaction_id",
            name="uq_decision_tenant_transaction",
        ),
    )
    op.create_index(
        "ix_decision_retention",
        "decision_records",
        ["tenant_id", "retention_expires_at"],
        unique=False,
    )

    op.create_table(
        "decision_audit_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_credential_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decision_records.tenant_id", "decision_records.decision_id"],
            name="fk_audit_tenant_decision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_audit_tenant_created",
        "decision_audit_events",
        ["tenant_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decision_records.tenant_id", "decision_records.decision_id"],
            name="fk_outbox_tenant_decision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "decision_id",
            "event_type",
            name="uq_outbox_decision_event",
        ),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_events",
        ["status", "created_at"],
        unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        tenant_expression = (
            "tenant_id = NULLIF("
            "current_setting('fraudguard.tenant_id', true), '')::uuid"
        )
        for table in (
            "decision_records",
            "decision_audit_events",
            "outbox_events",
        ):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_tenant_isolation ON {table} "
                f"USING ({tenant_expression}) "
                f"WITH CHECK ({tenant_expression})"
            )

        op.execute(
            """
            CREATE FUNCTION fraudguard_enforce_decision_transition()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.decision_id IS DISTINCT FROM OLD.decision_id
                    OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                    OR NEW.transaction_id IS DISTINCT FROM OLD.transaction_id
                    OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
                    OR NEW.fingerprint_key_version IS DISTINCT FROM OLD.fingerprint_key_version
                    OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
                    OR NEW.amount IS DISTINCT FROM OLD.amount
                    OR NEW.currency IS DISTINCT FROM OLD.currency
                    OR NEW.channel IS DISTINCT FROM OLD.channel
                    OR NEW.transaction_type IS DISTINCT FROM OLD.transaction_type
                    OR NEW.subject_fingerprints::text
                        IS DISTINCT FROM OLD.subject_fingerprints::text
                    OR NEW.request_id IS DISTINCT FROM OLD.request_id
                    OR NEW.received_at IS DISTINCT FROM OLD.received_at
                    OR NEW.retention_expires_at
                        IS DISTINCT FROM OLD.retention_expires_at
                THEN
                    RAISE EXCEPTION 'decision request fields are immutable';
                END IF;

                IF OLD.state = 'completed'
                    OR (OLD.state = 'failed' AND NOT OLD.retryable)
                THEN
                    RAISE EXCEPTION 'terminal decision records are immutable';
                END IF;

                IF OLD.state = 'failed' THEN
                    IF NOT OLD.retryable
                        OR NEW.state <> 'processing'
                        OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
                        OR NEW.attempt_count <> OLD.attempt_count + 1
                        OR NOT NEW.retryable
                        OR NEW.failure_code IS NOT NULL
                        OR NEW.response_json IS NOT NULL
                        OR NEW.completed_at IS NOT NULL
                    THEN
                        RAISE EXCEPTION 'invalid failed decision transition';
                    END IF;
                ELSIF OLD.state = 'processing' THEN
                    IF NEW.state = 'processing' THEN
                        IF OLD.lease_expires_at > CURRENT_TIMESTAMP
                            OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
                            OR NEW.attempt_count <> OLD.attempt_count + 1
                        THEN
                            RAISE EXCEPTION 'invalid processing lease reclaim';
                        END IF;
                    ELSIF NEW.state = 'completed' THEN
                        IF NEW.lease_token IS DISTINCT FROM OLD.lease_token
                            OR NEW.attempt_count <> OLD.attempt_count
                            OR NEW.retryable
                            OR NEW.response_json IS NULL
                            OR NEW.action IS NULL
                            OR NEW.completed_at IS NULL
                        THEN
                            RAISE EXCEPTION 'invalid completion transition';
                        END IF;
                    ELSIF NEW.state = 'failed' THEN
                        IF NEW.lease_token IS DISTINCT FROM OLD.lease_token
                            OR NEW.attempt_count <> OLD.attempt_count
                            OR NEW.failure_code IS NULL
                            OR NEW.completed_at IS NULL
                            OR (NEW.retryable AND NEW.response_json IS NOT NULL)
                            OR (NOT NEW.retryable AND NEW.response_json IS NULL)
                        THEN
                            RAISE EXCEPTION 'invalid failure transition';
                        END IF;
                    ELSE
                        RAISE EXCEPTION 'invalid decision state transition';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'invalid decision state';
                END IF;

                RETURN NEW;
            END;
            $$
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

        op.execute(
            """
            CREATE FUNCTION fraudguard_prevent_audit_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'decision audit events are append-only';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER decision_audit_events_append_only
            BEFORE UPDATE OR DELETE ON decision_audit_events
            FOR EACH ROW EXECUTE FUNCTION fraudguard_prevent_audit_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS decision_records_transition_guard "
            "ON decision_records"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS fraudguard_enforce_decision_transition()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS decision_audit_events_append_only "
            "ON decision_audit_events"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS fraudguard_prevent_audit_mutation()"
        )
    op.drop_index("ix_outbox_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(
        "ix_audit_tenant_created",
        table_name="decision_audit_events",
    )
    op.drop_table("decision_audit_events")
    op.drop_index("ix_decision_retention", table_name="decision_records")
    op.drop_table("decision_records")
    op.drop_index(
        op.f("ix_api_credentials_tenant_id"),
        table_name="api_credentials",
    )
    op.drop_table("api_credentials")
    op.drop_table("tenants")
