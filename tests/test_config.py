import base64

import pytest
from pydantic import SecretStr, ValidationError

from src.config import Settings, decode_secret


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def production_settings(**overrides):
    values = {
        "environment": "production",
        "auth_mode": "required",
        "database_url": "postgresql+psycopg://fraudguard:secret@db/fraudguard",
        "auto_create_schema": False,
        "api_key_peppers": {"v1": encoded_secret(b"a")},
        "data_pepper": encoded_secret(b"d"),
        "velocity_backend": "redis",
        "velocity_failure_mode": "fail_closed",
        "redis_url": "rediss://redis:6379/0",
        "velocity_warmup_seconds": 300,
    }
    values.update(overrides)
    return Settings(**values)


def test_local_defaults_are_explicitly_non_production():
    settings = Settings()
    assert settings.environment == "local"
    assert settings.auth_mode == "disabled"
    assert settings.database_dsn == "sqlite+pysqlite:///:memory:"
    assert settings.velocity_backend == "memory"


def test_production_configuration_accepts_required_backends_and_secrets():
    settings = production_settings()
    assert settings.key_namespace == "live"
    assert settings.api_key_pepper("v1") == b"a" * 32
    assert settings.fingerprint_key == b"d" * 32


def test_production_requires_an_explicit_independent_data_pepper():
    with pytest.raises(ValidationError, match="explicit unique data pepper"):
        production_settings(data_pepper=Settings().data_pepper)
    with pytest.raises(ValidationError, match="independent"):
        production_settings(data_pepper=encoded_secret(b"a"))


def test_data_pepper_keyring_retains_previous_versions_for_replay():
    settings = Settings(
        environment="test",
        data_pepper=encoded_secret(b"n"),
        data_pepper_version="v2",
        data_pepper_keyring={"v1": encoded_secret(b"o")},
    )
    assert settings.fingerprint_keys == {
        "v1": b"o" * 32,
        "v2": b"n" * 32,
    }


def test_data_pepper_keyring_rejects_a_conflicting_active_entry():
    with pytest.raises(ValidationError, match="disagrees"):
        Settings(
            environment="test",
            data_pepper=encoded_secret(b"n"),
            data_pepper_version="v2",
            data_pepper_keyring={"v2": encoded_secret(b"o")},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auth_mode", "disabled", "disabled authentication"),
        ("database_url", "sqlite:///fraudguard.db", "requires PostgreSQL"),
        ("velocity_backend", "memory", "requires Redis velocity"),
        ("velocity_failure_mode", "degraded_shadow", "must fail closed"),
        ("auto_create_schema", True, "require migrations"),
        ("velocity_warmup_seconds", 299, "300-second warmup"),
    ],
)
def test_production_rejects_unsafe_configuration(field, value, message):
    with pytest.raises(ValidationError, match=message):
        production_settings(**{field: value})


def test_required_auth_needs_an_active_pepper():
    with pytest.raises(ValidationError, match="at least one API-key pepper"):
        Settings(environment="test", auth_mode="required")
    with pytest.raises(ValidationError, match="active API-key pepper"):
        Settings(
            environment="test",
            auth_mode="required",
            api_key_peppers={"old": encoded_secret()},
        )


def test_redis_backend_requires_url_in_every_environment():
    with pytest.raises(ValidationError, match="redis_url"):
        Settings(velocity_backend="redis")


@pytest.mark.parametrize(
    "value",
    [SecretStr("not base64!"), SecretStr(encoded_secret()[:20])],
)
def test_security_secret_validation_rejects_invalid_or_short_values(value):
    with pytest.raises(ValueError):
        decode_secret(value)
