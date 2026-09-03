"""Construction of process-wide Phase 1 dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis import Redis

from .config import Settings
from .observability import Metrics
from .persistence import (
    CredentialRepository,
    Database,
    DecisionLedger,
    PolicyRepository,
)
from .rate_limit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from .security import ApiKeyVerifier
from .service import DecisionService
from .velocity import (
    HmacKeyer,
    InMemoryVelocityStore,
    RedisVelocityStore,
    VelocityStore,
)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    credentials: CredentialRepository
    ledger: DecisionLedger
    policies: PolicyRepository
    velocity: VelocityStore
    rate_limiter: RateLimiter
    verifier: ApiKeyVerifier
    service: DecisionService
    metrics: Metrics
    redis_client: Any | None = None

    def close(self) -> None:
        if self.redis_client is not None:
            self.redis_client.close()
        self.database.dispose()


def build_runtime(settings: Settings) -> Runtime:
    database = Database(
        settings.database_dsn,
        auto_create_schema=settings.auto_create_schema,
    )
    if settings.auth_mode == "disabled":
        database.ensure_tenant(
            settings.disabled_auth_tenant_id,
            slug="local-shadow",
            name="Local Shadow Tenant",
        )

    credentials = CredentialRepository(database)
    ledger = DecisionLedger(
        database,
        fingerprint_key_version=settings.data_pepper_version,
        fingerprint_keys=settings.fingerprint_keys,
    )
    policies = PolicyRepository(database)
    keyer = HmacKeyer(
        settings.fingerprint_key,
        key_version=settings.data_pepper_version,
    )
    redis_client = None
    if settings.velocity_backend == "redis":
        if settings.redis_dsn is None:
            raise ValueError("Redis URL is unavailable")
        redis_client = Redis.from_url(
            settings.redis_dsn,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
            health_check_interval=30,
            decode_responses=False,
        )
        velocity: VelocityStore = RedisVelocityStore(
            redis_client,
            keyer,
            warmup_ms=settings.velocity_warmup_seconds * 1000,
        )
        rate_limiter: RateLimiter = RedisRateLimiter(
            redis_client,
            keyer,
            rate_per_second=settings.decision_rate_limit_per_second,
            burst=settings.decision_rate_limit_burst,
        )
    else:
        velocity = InMemoryVelocityStore(
            keyer,
            warmup_ms=settings.velocity_warmup_seconds * 1000,
        )
        rate_limiter = InMemoryRateLimiter(
            keyer,
            rate_per_second=settings.decision_rate_limit_per_second,
            burst=settings.decision_rate_limit_burst,
        )

    verifier = ApiKeyVerifier(settings, credentials)
    service = DecisionService(
        settings=settings,
        ledger=ledger,
        policies=policies,
        velocity=velocity,
    )
    return Runtime(
        settings=settings,
        database=database,
        credentials=credentials,
        ledger=ledger,
        policies=policies,
        velocity=velocity,
        rate_limiter=rate_limiter,
        verifier=verifier,
        service=service,
        metrics=Metrics(),
        redis_client=redis_client,
    )
