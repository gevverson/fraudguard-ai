"""Tenant-aware persistence for credentials and durable decision records."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from .contracts import DecisionRequest
from .policy import (
    DEFAULT_POLICY,
    DEFAULT_POLICY_VERSION,
    TenantPolicy,
    policy_document,
    policy_sha256,
    validate_policy_version,
)
from .security import CredentialRecord, NewCredential

REQUIRED_SCHEMA_REVISION = "20260903_0002"
_READINESS_TENANT_ID = UUID("00000000-0000-4000-8000-000000000000")


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store timezone-aware UTC values consistently on SQLite and PostgreSQL."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        return dialect.type_descriptor(DateTime(timezone=dialect.name != "sqlite"))

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        normalized = value.astimezone(UTC)
        return normalized.replace(tzinfo=None) if dialect.name == "sqlite" else normalized

    def process_result_value(
        self, value: datetime | None, _dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_days: Mapped[int] = mapped_column(nullable=False, default=30)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    __table_args__ = (
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="ck_tenant_retention"),
    )


class TenantPolicyVersionRow(Base):
    __tablename__ = "tenant_policy_versions"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class TenantPolicyActivationRow(Base):
    __tablename__ = "tenant_policy_activations"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    active_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(nullable=False)
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "active_policy_version"],
            [
                "tenant_policy_versions.tenant_id",
                "tenant_policy_versions.policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_policy_activation_version",
        ),
        CheckConstraint("generation >= 1", name="ck_policy_activation_generation"),
    )


class TenantPolicyActivationEventRow(Base):
    __tablename__ = "tenant_policy_activation_events"

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_policy_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    generation: Mapped[int] = mapped_column(nullable=False)
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "policy_version"],
            [
                "tenant_policy_versions.tenant_id",
                "tenant_policy_versions.policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_policy_event_version",
        ),
        UniqueConstraint(
            "tenant_id", "generation", name="uq_policy_event_tenant_generation"
        ),
        CheckConstraint("generation >= 1", name="ck_policy_event_generation"),
        Index("ix_policy_event_tenant_created", "tenant_id", "created_at"),
    )


def _seed_default_policy(
    session: Session, tenant_id: UUID, *, actor: str = "system:tenant-created"
) -> None:
    now = utc_now()
    session.add(
        TenantPolicyVersionRow(
            tenant_id=tenant_id,
            policy_version=DEFAULT_POLICY_VERSION,
            policy_schema_version=DEFAULT_POLICY.policy_schema_version,
            policy_document=policy_document(DEFAULT_POLICY),
            document_sha256=policy_sha256(DEFAULT_POLICY),
            created_by=actor,
            created_at=now,
        )
    )
    session.flush()
    session.add(
        TenantPolicyActivationRow(
            tenant_id=tenant_id,
            active_policy_version=DEFAULT_POLICY_VERSION,
            generation=1,
            activated_by=actor,
            activated_at=now,
        )
    )
    session.add(
        TenantPolicyActivationEventRow(
            event_id=uuid4(),
            tenant_id=tenant_id,
            policy_version=DEFAULT_POLICY_VERSION,
            previous_policy_version=None,
            generation=1,
            activated_by=actor,
            created_at=now,
        )
    )


class ApiCredentialRow(Base):
    __tablename__ = "api_credentials"

    credential_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    namespace: Mapped[str] = mapped_column(String(8), nullable=False)
    secret_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    pepper_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    __table_args__ = (
        CheckConstraint("namespace IN ('live', 'test')", name="ck_credential_namespace"),
        CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_credential_status"
        ),
    )


class DecisionRecordRow(Base):
    __tablename__ = "decision_records"

    decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    transaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=1)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_fingerprints: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)

    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    service_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enforced: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rule_hits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    retention_expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "policy_version"],
            [
                "tenant_policy_versions.tenant_id",
                "tenant_policy_versions.policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_decision_policy_version",
        ),
        UniqueConstraint(
            "tenant_id", "transaction_id", name="uq_decision_tenant_transaction"
        ),
        UniqueConstraint(
            "tenant_id", "decision_id", name="uq_decision_tenant_decision"
        ),
        CheckConstraint(
            "state IN ('processing', 'completed', 'failed')",
            name="ck_decision_state",
        ),
        CheckConstraint(
            "action IS NULL OR action IN ('approve', 'review', 'decline')",
            name="ck_decision_action",
        ),
        Index(
            "ix_decision_retention",
            "tenant_id",
            "retention_expires_at",
        ),
    )


class DecisionAuditRow(Base):
    __tablename__ = "decision_audit_events"

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_credential_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decision_records.tenant_id", "decision_records.decision_id"],
            ondelete="CASCADE",
            name="fk_audit_tenant_decision",
        ),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
    )


class OutboxRow(Base):
    __tablename__ = "outbox_events"

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["decision_records.tenant_id", "decision_records.decision_id"],
            ondelete="CASCADE",
            name="fk_outbox_tenant_decision",
        ),
        UniqueConstraint(
            "decision_id", "event_type", name="uq_outbox_decision_event"
        ),
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_outbox_status",
        ),
        Index("ix_outbox_pending", "status", "created_at"),
    )


class PersistenceError(RuntimeError):
    """Base exception for storage failures safe to map at the API boundary."""


class PolicyUnavailable(PersistenceError):
    pass


class PolicyActivationConflict(PersistenceError):
    pass


class LedgerUnavailable(PersistenceError):
    pass


class IdempotencyConflict(PersistenceError):
    pass


class DecisionInProgress(PersistenceError):
    pass


class DecisionFailed(PersistenceError):
    """A terminal decision failure whose stored response must be replayed."""

    def __init__(
        self,
        *,
        decision_id: UUID,
        response_json: str,
        replayed: bool,
    ) -> None:
        super().__init__("decision evaluation failed")
        self.decision_id = decision_id
        self.response_json = response_json
        self.replayed = replayed


class LostDecisionLease(PersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    tenant_id: UUID
    policy_version: str
    policy: TenantPolicy
    document_sha256: str


@dataclass(frozen=True, slots=True)
class ActivePolicy:
    snapshot: PolicySnapshot
    generation: int


@dataclass(frozen=True, slots=True)
class DecisionClaim:
    decision_id: UUID
    tenant_id: UUID
    transaction_id: str
    lease_token: UUID
    policy_version: str


@dataclass(frozen=True, slots=True)
class StoredDecision:
    decision_id: UUID
    response_json: str
    replayed: bool


ReserveResult = DecisionClaim | StoredDecision


def _strict_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_request_bytes(payment: DecisionRequest) -> bytes:
    """Serialize semantic fields rather than hashing raw caller JSON."""

    identifiers = payment.identifiers.model_dump(mode="json", exclude_none=True)
    document = {
        "amount": format(payment.amount.quantize(Decimal("0.01")), ".2f"),
        "channel": payment.channel.value,
        "currency": payment.currency,
        "identifiers": dict(sorted(identifiers.items())),
        "occurred_at": payment.occurred_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "transaction_id": payment.transaction_id,
        "transaction_type": payment.transaction_type.value,
    }
    return _strict_json(document).encode()


def request_fingerprint(payment: DecisionRequest, key: bytes) -> str:
    return hmac.new(key, canonical_request_bytes(payment), hashlib.sha256).hexdigest()


def subject_fingerprints(payment: DecisionRequest, key: bytes) -> dict[str, str]:
    values = payment.identifiers.model_dump(mode="json", exclude_none=True)
    return {
        field: hmac.new(
            key,
            f"subject:{field}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        for field, value in sorted(values.items())
        if field != "merchant_id"
    }


def create_db_engine(database_url: str) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 5}
        if database_url.endswith(":memory:"):
            options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if not database_url.endswith(":memory:"):
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


class Database:
    def __init__(self, database_url: str, *, auto_create_schema: bool) -> None:
        self.engine = create_db_engine(database_url)
        self._auto_create_schema = auto_create_schema
        self.sessions = sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        )
        if auto_create_schema:
            Base.metadata.create_all(self.engine)

    @property
    def is_postgresql(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def ensure_tenant(self, tenant_id: UUID, *, slug: str, name: str) -> None:
        try:
            with self.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                if session.get(TenantRow, tenant_id) is None:
                    session.add(
                        TenantRow(
                            id=tenant_id,
                            slug=slug,
                            name=name,
                            active=True,
                            retention_days=30,
                        )
                    )
                    session.flush()
                    _seed_default_policy(session, tenant_id)
        except SQLAlchemyError as exc:
            raise PersistenceError("could not initialize tenant") from exc

    def ready(self, *, require_runtime_privileges: bool = True) -> bool:
        try:
            expected = {
                "tenants",
                "api_credentials",
                "tenant_policy_versions",
                "tenant_policy_activations",
                "tenant_policy_activation_events",
                "decision_records",
                "decision_audit_events",
                "outbox_events",
            }
            if not expected.issubset(set(inspect(self.engine).get_table_names())):
                return False
            with self.engine.begin() as connection:
                connection.execute(text("SELECT 1"))
                if not self._auto_create_schema:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one_or_none()
                    if revision != REQUIRED_SCHEMA_REVISION:
                        return False

                if self.is_postgresql and require_runtime_privileges:
                    connection.execute(
                        text(
                            "SELECT set_config("
                            "'fraudguard.tenant_id', :tenant_id, true)"
                        ),
                        {"tenant_id": str(_READINESS_TENANT_ID)},
                    )
                    required_privileges = {
                        "tenants": ("SELECT",),
                        "api_credentials": ("SELECT",),
                        "tenant_policy_versions": ("SELECT",),
                        "tenant_policy_activations": ("SELECT",),
                        "tenant_policy_activation_events": ("SELECT",),
                        "decision_records": ("SELECT", "INSERT", "UPDATE"),
                        "decision_audit_events": ("SELECT", "INSERT"),
                        "outbox_events": ("SELECT", "INSERT"),
                    }
                    for table_name, privileges in required_privileges.items():
                        for privilege in privileges:
                            granted = connection.execute(
                                text(
                                    "SELECT has_table_privilege("
                                    "current_user, :table_name, :privilege)"
                                ),
                                {
                                    "table_name": f"public.{table_name}",
                                    "privilege": privilege,
                                },
                            ).scalar_one()
                            if not granted:
                                return False

                if require_runtime_privileges:
                    for statement in (
                        select(TenantRow).limit(0),
                        select(ApiCredentialRow).limit(0),
                        select(TenantPolicyVersionRow).limit(0),
                        select(TenantPolicyActivationRow).limit(0),
                        select(TenantPolicyActivationEventRow).limit(0),
                        select(DecisionRecordRow).limit(0),
                        select(DecisionAuditRow).limit(0),
                        select(OutboxRow).limit(0),
                    ):
                        connection.execute(statement)
            return True
        except SQLAlchemyError:
            return False

    def dispose(self) -> None:
        self.engine.dispose()


def _set_tenant_context(session: Session, tenant_id: UUID) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('fraudguard.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )


class CredentialRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_tenant(self, *, slug: str, name: str, tenant_id: UUID | None = None) -> UUID:
        clean_slug = slug.strip().lower()
        clean_name = name.strip()
        if not clean_slug or not clean_name:
            raise ValueError("tenant slug and name are required")
        new_id = tenant_id or uuid4()
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, new_id)
                session.add(
                    TenantRow(
                        id=new_id,
                        slug=clean_slug,
                        name=clean_name,
                        active=True,
                        retention_days=30,
                    )
                )
                session.flush()
                _seed_default_policy(session, new_id, actor="operator:tenant-created")
            return new_id
        except IntegrityError as exc:
            raise ValueError("tenant slug or identifier already exists") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError("could not create tenant") from exc

    def insert_credential(self, credential: NewCredential) -> None:
        try:
            with self._database.sessions.begin() as session:
                session.add(
                    ApiCredentialRow(
                        credential_id=credential.credential_id,
                        tenant_id=credential.tenant_id,
                        key_id=credential.key_id,
                        namespace=credential.namespace,
                        secret_digest=credential.secret_digest,
                        pepper_version=credential.pepper_version,
                        scopes=sorted(credential.scopes),
                        name=credential.name,
                        status="active",
                        expires_at=credential.expires_at,
                    )
                )
        except IntegrityError as exc:
            raise ValueError("credential identifier already exists") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError("could not persist credential") from exc

    def get_by_key_id(self, key_id: str) -> CredentialRecord | None:
        try:
            with self._database.sessions() as session:
                result = session.execute(
                    select(ApiCredentialRow, TenantRow.active)
                    .join(TenantRow, TenantRow.id == ApiCredentialRow.tenant_id)
                    .where(ApiCredentialRow.key_id == key_id)
                ).one_or_none()
                if result is None:
                    return None
                row, tenant_active = result
                effective_status = row.status if tenant_active else "revoked"
                return CredentialRecord(
                    credential_id=row.credential_id,
                    tenant_id=row.tenant_id,
                    key_id=row.key_id,
                    namespace=row.namespace,
                    secret_digest=row.secret_digest,
                    pepper_version=row.pepper_version,
                    scopes=frozenset(row.scopes),
                    status=effective_status,
                    expires_at=row.expires_at,
                )
        except SQLAlchemyError as exc:
            raise PersistenceError("credential store is unavailable") from exc

    def revoke(self, credential_id: UUID) -> bool:
        try:
            with self._database.sessions.begin() as session:
                row = session.get(ApiCredentialRow, credential_id)
                if row is None:
                    return False
                row.status = "revoked"
                row.revoked_at = utc_now()
                return True
        except SQLAlchemyError as exc:
            raise PersistenceError("could not revoke credential") from exc


def _clean_policy_actor(actor: str) -> str:
    value = actor.strip()
    if not value or len(value) > 128:
        raise ValueError("policy actor must contain 1-128 characters")
    return value


def _policy_snapshot(row: TenantPolicyVersionRow) -> PolicySnapshot:
    try:
        policy = TenantPolicy.model_validate(row.policy_document)
    except ValueError as exc:
        raise PolicyUnavailable("stored policy document is invalid") from exc
    digest = policy_sha256(policy)
    if not hmac.compare_digest(digest, row.document_sha256):
        raise PolicyUnavailable("stored policy document failed its integrity check")
    if policy.policy_schema_version != row.policy_schema_version:
        raise PolicyUnavailable("stored policy schema version is inconsistent")
    return PolicySnapshot(
        tenant_id=row.tenant_id,
        policy_version=row.policy_version,
        policy=policy,
        document_sha256=digest,
    )


class PolicyRepository:
    """Immutable policy versions plus an atomic per-tenant active pointer."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get_version(self, tenant_id: UUID, policy_version: str) -> PolicySnapshot:
        version = validate_policy_version(policy_version)
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                row = session.get(TenantPolicyVersionRow, (tenant_id, version))
                if row is None:
                    raise PolicyUnavailable("policy version is unavailable")
                return _policy_snapshot(row)
        except PolicyUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy store is unavailable") from exc

    def resolve_active(self, tenant_id: UUID) -> ActivePolicy:
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                activation = session.get(TenantPolicyActivationRow, tenant_id)
                if activation is None:
                    raise PolicyUnavailable("tenant has no active policy")
                row = session.get(
                    TenantPolicyVersionRow,
                    (tenant_id, activation.active_policy_version),
                )
                if row is None:
                    raise PolicyUnavailable("active policy version is unavailable")
                return ActivePolicy(_policy_snapshot(row), activation.generation)
        except PolicyUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy store is unavailable") from exc

    def list_versions(self, tenant_id: UUID) -> list[PolicySnapshot]:
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                rows = session.scalars(
                    select(TenantPolicyVersionRow)
                    .where(TenantPolicyVersionRow.tenant_id == tenant_id)
                    .order_by(TenantPolicyVersionRow.created_at)
                ).all()
                return [_policy_snapshot(row) for row in rows]
        except PolicyUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy store is unavailable") from exc

    def create_version(
        self,
        *,
        tenant_id: UUID,
        policy_version: str,
        policy: TenantPolicy,
        actor: str,
    ) -> PolicySnapshot:
        version = validate_policy_version(policy_version)
        clean_actor = _clean_policy_actor(actor)
        snapshot = PolicySnapshot(
            tenant_id=tenant_id,
            policy_version=version,
            policy=policy,
            document_sha256=policy_sha256(policy),
        )
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                tenant = session.get(TenantRow, tenant_id)
                if tenant is None or not tenant.active:
                    raise PolicyUnavailable("tenant is unavailable")
                session.add(
                    TenantPolicyVersionRow(
                        tenant_id=tenant_id,
                        policy_version=version,
                        policy_schema_version=policy.policy_schema_version,
                        policy_document=policy_document(policy),
                        document_sha256=snapshot.document_sha256,
                        created_by=clean_actor,
                        created_at=utc_now(),
                    )
                )
            return snapshot
        except IntegrityError as exc:
            raise ValueError("policy version already exists") from exc
        except PolicyUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy version could not be created") from exc

    def _activate_in_session(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        policy_version: str,
        actor: str,
        expected_generation: int | None,
    ) -> ActivePolicy:
        activation = session.execute(
            select(TenantPolicyActivationRow)
            .where(TenantPolicyActivationRow.tenant_id == tenant_id)
            .with_for_update()
        ).scalar_one_or_none()
        if activation is None:
            raise PolicyUnavailable("tenant has no active policy")
        if (
            expected_generation is not None
            and activation.generation != expected_generation
        ):
            raise PolicyActivationConflict(
                "active policy changed; reload before activating"
            )
        row = session.get(
            TenantPolicyVersionRow, (tenant_id, policy_version)
        )
        if row is None:
            raise PolicyUnavailable("target policy version is unavailable")
        snapshot = _policy_snapshot(row)
        if activation.active_policy_version == policy_version:
            return ActivePolicy(snapshot, activation.generation)

        previous = activation.active_policy_version
        generation = activation.generation + 1
        now = utc_now()
        activation.active_policy_version = policy_version
        activation.generation = generation
        activation.activated_by = actor
        activation.activated_at = now
        session.add(
            TenantPolicyActivationEventRow(
                event_id=uuid4(),
                tenant_id=tenant_id,
                policy_version=policy_version,
                previous_policy_version=previous,
                generation=generation,
                activated_by=actor,
                created_at=now,
            )
        )
        return ActivePolicy(snapshot, generation)

    def activate(
        self,
        *,
        tenant_id: UUID,
        policy_version: str,
        actor: str,
        expected_generation: int | None = None,
    ) -> ActivePolicy:
        version = validate_policy_version(policy_version)
        clean_actor = _clean_policy_actor(actor)
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                return self._activate_in_session(
                    session,
                    tenant_id=tenant_id,
                    policy_version=version,
                    actor=clean_actor,
                    expected_generation=expected_generation,
                )
        except (PolicyActivationConflict, PolicyUnavailable):
            raise
        except IntegrityError as exc:
            raise PolicyActivationConflict(
                "another operator changed the active policy"
            ) from exc
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy activation could not be committed") from exc

    def rollback(
        self,
        *,
        tenant_id: UUID,
        actor: str,
        expected_generation: int | None = None,
    ) -> ActivePolicy:
        clean_actor = _clean_policy_actor(actor)
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                activation = session.execute(
                    select(TenantPolicyActivationRow)
                    .where(TenantPolicyActivationRow.tenant_id == tenant_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if activation is None:
                    raise PolicyUnavailable("tenant has no active policy")
                if (
                    expected_generation is not None
                    and activation.generation != expected_generation
                ):
                    raise PolicyActivationConflict(
                        "active policy changed; reload before rollback"
                    )
                event_row = session.execute(
                    select(TenantPolicyActivationEventRow).where(
                        TenantPolicyActivationEventRow.tenant_id == tenant_id,
                        TenantPolicyActivationEventRow.generation
                        == activation.generation,
                    )
                ).scalar_one_or_none()
                if event_row is None or event_row.previous_policy_version is None:
                    raise PolicyActivationConflict(
                        "active policy has no previous version to restore"
                    )
                return self._activate_in_session(
                    session,
                    tenant_id=tenant_id,
                    policy_version=event_row.previous_policy_version,
                    actor=clean_actor,
                    expected_generation=activation.generation,
                )
        except (PolicyActivationConflict, PolicyUnavailable):
            raise
        except IntegrityError as exc:
            raise PolicyActivationConflict(
                "another operator changed the active policy"
            ) from exc
        except SQLAlchemyError as exc:
            raise PolicyUnavailable("policy rollback could not be committed") from exc


class DecisionLedger:
    """Lease-based idempotent decision state machine."""

    def __init__(
        self,
        database: Database,
        *,
        fingerprint_key_version: str,
        fingerprint_keys: Mapping[str, bytes] | None = None,
        lease_seconds: int = 15,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._fingerprint_key_version = fingerprint_key_version
        self._fingerprint_keys = dict(fingerprint_keys or {})
        self._lease_seconds = lease_seconds
        self._clock = clock

    def reserve(
        self,
        *,
        tenant_id: UUID,
        credential_id: UUID,
        payment: DecisionRequest,
        fingerprint: str,
        subject_hashes: dict[str, str],
        request_id: str,
    ) -> ReserveResult:
        now = self._clock()
        decision_id = uuid4()
        lease_token = uuid4()
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                tenant = session.get(TenantRow, tenant_id)
                if tenant is None or not tenant.active:
                    raise PersistenceError("tenant is unavailable")
                activation = session.execute(
                    select(TenantPolicyActivationRow)
                    .where(TenantPolicyActivationRow.tenant_id == tenant_id)
                    .with_for_update(read=True)
                ).scalar_one_or_none()
                if activation is None:
                    raise PolicyUnavailable("tenant has no active policy")
                row = DecisionRecordRow(
                    decision_id=decision_id,
                    tenant_id=tenant_id,
                    transaction_id=payment.transaction_id,
                    request_fingerprint=fingerprint,
                    fingerprint_key_version=self._fingerprint_key_version,
                    state="processing",
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    attempt_count=1,
                    retryable=True,
                    occurred_at=payment.occurred_at,
                    amount=payment.amount,
                    currency=payment.currency,
                    channel=payment.channel.value,
                    transaction_type=payment.transaction_type.value,
                    subject_fingerprints=subject_hashes,
                    policy_version=activation.active_policy_version,
                    request_id=request_id,
                    received_at=now,
                    retention_expires_at=now + timedelta(days=tenant.retention_days),
                )
                session.add(row)
                # No ORM relationship is needed here, so make the FK ordering
                # explicit before appending the first audit event.
                session.flush()
                session.add(
                    DecisionAuditRow(
                        event_id=uuid4(),
                        tenant_id=tenant_id,
                        decision_id=decision_id,
                        event_type="reserved",
                        actor_credential_id=credential_id,
                        request_id=request_id,
                        metadata_json={
                            "policy_version": activation.active_policy_version
                        },
                        created_at=now,
                    )
                )
            return DecisionClaim(
                decision_id=decision_id,
                tenant_id=tenant_id,
                transaction_id=payment.transaction_id,
                lease_token=lease_token,
                policy_version=activation.active_policy_version,
            )
        except IntegrityError:
            return self._resolve_existing(
                tenant_id=tenant_id,
                credential_id=credential_id,
                transaction_id=payment.transaction_id,
                payment=payment,
                fingerprint=fingerprint,
                request_id=request_id,
            )
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("decision ledger is unavailable") from exc

    def _resolve_existing(
        self,
        *,
        tenant_id: UUID,
        credential_id: UUID,
        transaction_id: str,
        payment: DecisionRequest,
        fingerprint: str,
        request_id: str,
    ) -> ReserveResult:
        now = self._clock()
        new_lease = uuid4()
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, tenant_id)
                row = session.execute(
                    select(DecisionRecordRow).where(
                        DecisionRecordRow.tenant_id == tenant_id,
                        DecisionRecordRow.transaction_id == transaction_id,
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise LedgerUnavailable("idempotency collision could not be resolved")
                fingerprint_key = self._fingerprint_keys.get(
                    row.fingerprint_key_version
                )
                if fingerprint_key is not None:
                    candidate_fingerprint = request_fingerprint(
                        payment, fingerprint_key
                    )
                elif row.fingerprint_key_version == self._fingerprint_key_version:
                    candidate_fingerprint = fingerprint
                else:
                    raise LedgerUnavailable(
                        "stored fingerprint key version is unavailable"
                    )
                if not hmac.compare_digest(
                    row.request_fingerprint, candidate_fingerprint
                ):
                    raise IdempotencyConflict(
                        "transaction_id was already used with a different request"
                    )
                if row.state == "completed":
                    if row.response_json is None:
                        raise LedgerUnavailable("completed decision has no response")
                    return StoredDecision(
                        decision_id=row.decision_id,
                        response_json=row.response_json,
                        replayed=True,
                    )
                if row.state == "failed" and not row.retryable:
                    if row.response_json is None:
                        raise LedgerUnavailable(
                            "terminal decision failure has no stored response"
                        )
                    raise DecisionFailed(
                        decision_id=row.decision_id,
                        response_json=row.response_json,
                        replayed=True,
                    )
                can_reclaim = (
                    row.state == "processing" and row.lease_expires_at <= now
                ) or (row.state == "failed" and row.retryable)
                if not can_reclaim:
                    raise DecisionInProgress("decision is already being processed")

                previous_lease = row.lease_token
                result = session.execute(
                    update(DecisionRecordRow)
                    .where(
                        DecisionRecordRow.tenant_id == tenant_id,
                        DecisionRecordRow.decision_id == row.decision_id,
                        DecisionRecordRow.lease_token == previous_lease,
                        DecisionRecordRow.state == row.state,
                    )
                    .values(
                        state="processing",
                        lease_token=new_lease,
                        lease_expires_at=now
                        + timedelta(seconds=self._lease_seconds),
                        attempt_count=row.attempt_count + 1,
                        retryable=True,
                        failure_code=None,
                        response_json=None,
                        completed_at=None,
                    )
                )
                if result.rowcount != 1:
                    raise DecisionInProgress("another worker reclaimed the decision")
                session.add(
                    DecisionAuditRow(
                        event_id=uuid4(),
                        tenant_id=tenant_id,
                        decision_id=row.decision_id,
                        event_type="lease_reclaimed",
                        actor_credential_id=credential_id,
                        request_id=request_id,
                        metadata_json={},
                        created_at=now,
                    )
                )
                return DecisionClaim(
                    decision_id=row.decision_id,
                    tenant_id=tenant_id,
                    transaction_id=transaction_id,
                    lease_token=new_lease,
                    policy_version=row.policy_version,
                )
        except (
            DecisionFailed,
            IdempotencyConflict,
            DecisionInProgress,
            LedgerUnavailable,
        ):
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("decision ledger is unavailable") from exc

    def complete(
        self,
        claim: DecisionClaim,
        *,
        credential_id: UUID,
        request_id: str,
        response_json: str,
        action: str,
        mode: str,
        schema_version: str,
        feature_version: str,
        feature_status: str,
        model_version: str | None,
        service_version: str,
        enforced: bool,
        rule_hits_json: str,
    ) -> StoredDecision:
        now = self._clock()
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, claim.tenant_id)
                result = session.execute(
                    update(DecisionRecordRow)
                    .where(
                        DecisionRecordRow.tenant_id == claim.tenant_id,
                        DecisionRecordRow.decision_id == claim.decision_id,
                        DecisionRecordRow.state == "processing",
                        DecisionRecordRow.lease_token == claim.lease_token,
                        DecisionRecordRow.policy_version == claim.policy_version,
                    )
                    .values(
                        state="completed",
                        retryable=False,
                        action=action,
                        mode=mode,
                        schema_version=schema_version,
                        feature_version=feature_version,
                        feature_status=feature_status,
                        model_version=model_version,
                        service_version=service_version,
                        enforced=enforced,
                        rule_hits_json=rule_hits_json,
                        response_json=response_json,
                        completed_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise LostDecisionLease("decision lease is no longer owned")
                session.add_all(
                    [
                        DecisionAuditRow(
                            event_id=uuid4(),
                            tenant_id=claim.tenant_id,
                            decision_id=claim.decision_id,
                            event_type="completed",
                            actor_credential_id=credential_id,
                            request_id=request_id,
                            metadata_json={
                                "feature_status": feature_status,
                                "policy_version": claim.policy_version,
                            },
                            created_at=now,
                        ),
                        OutboxRow(
                            event_id=uuid4(),
                            tenant_id=claim.tenant_id,
                            decision_id=claim.decision_id,
                            event_type="decision.completed",
                            payload_json=response_json,
                            status="pending",
                            attempts=0,
                            created_at=now,
                        ),
                    ]
                )
            return StoredDecision(
                decision_id=claim.decision_id,
                response_json=response_json,
                replayed=False,
            )
        except LostDecisionLease:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("decision completion could not be committed") from exc

    def fail(
        self,
        claim: DecisionClaim,
        *,
        credential_id: UUID,
        request_id: str,
        failure_code: str,
        retryable: bool,
        response_json: str | None = None,
    ) -> None:
        if retryable and response_json is not None:
            raise ValueError("retryable failures cannot store a terminal response")
        if not retryable and response_json is None:
            raise ValueError("terminal failures require a stored response")
        now = self._clock()
        try:
            with self._database.sessions.begin() as session:
                _set_tenant_context(session, claim.tenant_id)
                result = session.execute(
                    update(DecisionRecordRow)
                    .where(
                        DecisionRecordRow.tenant_id == claim.tenant_id,
                        DecisionRecordRow.decision_id == claim.decision_id,
                        DecisionRecordRow.state == "processing",
                        DecisionRecordRow.lease_token == claim.lease_token,
                    )
                    .values(
                        state="failed",
                        retryable=retryable,
                        failure_code=failure_code[:64],
                        response_json=response_json,
                        completed_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise LostDecisionLease("decision lease is no longer owned")
                session.add(
                    DecisionAuditRow(
                        event_id=uuid4(),
                        tenant_id=claim.tenant_id,
                        decision_id=claim.decision_id,
                        event_type="failed",
                        actor_credential_id=credential_id,
                        request_id=request_id,
                        metadata_json={
                            "failure_code": failure_code[:64],
                            "retryable": retryable,
                        },
                        created_at=now,
                    )
                )
        except LostDecisionLease:
            raise
        except SQLAlchemyError as exc:
            raise LedgerUnavailable("decision failure could not be recorded") from exc
