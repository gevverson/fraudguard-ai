"""Typed runtime configuration with production fail-closed checks."""

from __future__ import annotations

import base64
from functools import lru_cache
import hmac
import re
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
AuthMode = Literal["required", "disabled"]
VelocityBackend = Literal["memory", "redis"]
VelocityFailureMode = Literal["fail_closed", "degraded_shadow"]

_LOCAL_DATA_PEPPER = (
    "ZGV2ZWxvcG1lbnQtb25seS1kYXRhLXBlcHBlci1ub3QtZm9yLXByb2R1Y3Rpb24"
)


def decode_secret(value: SecretStr) -> bytes:
    """Decode a base64url secret and require at least 256 bits."""

    encoded = value.get_secret_value()
    padded = encoded + ("=" * (-len(encoded) % 4))
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError("security peppers must be base64url") from exc
    if len(decoded) < 32:
        raise ValueError("security peppers must contain at least 256 bits")
    return decoded


class Settings(BaseSettings):
    """FraudGuard process settings.

    Local defaults are deliberately self-contained. Staging and production
    reject disabled authentication, SQLite, in-memory velocity, automatic
    schema creation, and missing secrets.
    """

    model_config = SettingsConfigDict(
        env_prefix="FRAUDGUARD_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    auth_mode: AuthMode = "disabled"
    database_url: SecretStr = SecretStr("sqlite+pysqlite:///:memory:")
    auto_create_schema: bool = True

    api_key_peppers: dict[str, SecretStr] = Field(default_factory=dict)
    active_api_key_pepper_version: str = "v1"
    data_pepper: SecretStr = SecretStr(_LOCAL_DATA_PEPPER)
    data_pepper_version: str = "v1"
    data_pepper_keyring: dict[str, SecretStr] = Field(default_factory=dict)

    velocity_backend: VelocityBackend = "memory"
    velocity_failure_mode: VelocityFailureMode = "degraded_shadow"
    redis_url: SecretStr | None = None
    velocity_warmup_seconds: int = Field(default=0, ge=0, le=3600)

    request_max_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    decision_rate_limit_per_second: int = Field(default=50, ge=1, le=10_000)
    decision_rate_limit_burst: int = Field(default=100, ge=1, le=100_000)
    service_version: str = "0.4.0"

    disabled_auth_tenant_id: UUID = UUID(
        "00000000-0000-4000-8000-000000000001"
    )
    disabled_auth_credential_id: UUID = UUID(
        "00000000-0000-4000-8000-000000000002"
    )

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "Settings":
        decode_secret(self.data_pepper)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", self.data_pepper_version) is None:
            raise ValueError("data pepper version is invalid")
        for version, pepper in self.data_pepper_keyring.items():
            if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", version) is None:
                raise ValueError("data pepper keyring contains an invalid version")
            decode_secret(pepper)
        configured_current = self.data_pepper_keyring.get(self.data_pepper_version)
        if configured_current is not None and decode_secret(
            configured_current
        ) != decode_secret(self.data_pepper):
            raise ValueError("active data pepper disagrees with its keyring entry")

        if self.auth_mode == "disabled" and self.environment not in {"local", "test"}:
            raise ValueError("disabled authentication is allowed only in local/test")

        if self.auth_mode == "required":
            if not self.api_key_peppers:
                raise ValueError("at least one API-key pepper is required")
            if self.active_api_key_pepper_version not in self.api_key_peppers:
                raise ValueError("active API-key pepper version is unavailable")
            for pepper in self.api_key_peppers.values():
                decode_secret(pepper)

        if self.velocity_backend == "redis" and self.redis_url is None:
            raise ValueError("redis_url is required for the Redis velocity backend")

        if self.environment in {"staging", "production"}:
            database_url = self.database_url.get_secret_value()
            data_secret = decode_secret(self.data_pepper)
            if self.auth_mode != "required":
                raise ValueError("authentication is mandatory outside local/test")
            if self.data_pepper.get_secret_value() == _LOCAL_DATA_PEPPER:
                raise ValueError(
                    "staging/production requires an explicit unique data pepper"
                )
            if any(
                hmac.compare_digest(data_secret, decode_secret(pepper))
                for pepper in self.api_key_peppers.values()
            ):
                raise ValueError(
                    "the data pepper must be independent from API-key peppers"
                )
            if not database_url.startswith(("postgresql://", "postgresql+")):
                raise ValueError("staging/production requires PostgreSQL")
            if self.velocity_backend != "redis":
                raise ValueError("staging/production requires Redis velocity")
            if self.velocity_failure_mode != "fail_closed":
                raise ValueError("staging/production velocity must fail closed")
            if self.auto_create_schema:
                raise ValueError("staging/production schema changes require migrations")
            if self.velocity_warmup_seconds < 300:
                raise ValueError("staging/production requires a 300-second warmup")

        return self

    @property
    def key_namespace(self) -> Literal["live", "test"]:
        return "live" if self.environment in {"staging", "production"} else "test"

    @property
    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def redis_dsn(self) -> str | None:
        return self.redis_url.get_secret_value() if self.redis_url else None

    @property
    def fingerprint_key(self) -> bytes:
        return decode_secret(self.data_pepper)

    @property
    def fingerprint_keys(self) -> dict[str, bytes]:
        keys = {
            version: decode_secret(pepper)
            for version, pepper in self.data_pepper_keyring.items()
        }
        keys[self.data_pepper_version] = self.fingerprint_key
        return keys

    def api_key_pepper(self, version: str) -> bytes | None:
        pepper = self.api_key_peppers.get(version)
        return decode_secret(pepper) if pepper is not None else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
