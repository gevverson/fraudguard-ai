"""Opaque per-tenant API-key authentication and provisioning."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterable, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

_KEY_PATTERN = re.compile(
    r"^fg_(?P<namespace>live|test)_"
    r"(?P<key_id>[A-Za-z0-9_-]{20,32})\."
    r"(?P<secret>[A-Za-z0-9_-]{43,64})$"
)
_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{2,63}$")
_DUMMY_DIGEST = bytes(hashlib.sha256().digest_size)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: UUID
    credential_id: UUID
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    credential_id: UUID
    tenant_id: UUID
    key_id: str
    namespace: str
    secret_digest: bytes
    pepper_version: str
    scopes: frozenset[str]
    status: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewCredential:
    credential_id: UUID
    tenant_id: UUID
    key_id: str
    namespace: str
    secret_digest: bytes
    pepper_version: str
    scopes: frozenset[str]
    name: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    plaintext: str
    credential: NewCredential


class CredentialReader(Protocol):
    def get_by_key_id(self, key_id: str) -> CredentialRecord | None: ...


class CredentialWriter(Protocol):
    def insert_credential(self, credential: NewCredential) -> None: ...


def _digest(pepper: bytes, namespace: str, key_id: str, secret: str) -> bytes:
    message = f"{namespace}:{key_id}:{secret}".encode()
    return hmac.digest(pepper, message, "sha256")


def _normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    normalized = frozenset(scopes)
    if not normalized or any(
        _SCOPE_PATTERN.fullmatch(scope) is None for scope in normalized
    ):
        raise ValueError("one or more API-key scopes are invalid")
    return normalized


def issue_api_key(
    *,
    settings: Settings,
    tenant_id: UUID,
    name: str,
    scopes: Iterable[str],
    expires_at: datetime | None = None,
) -> IssuedApiKey:
    """Create a credential whose plaintext is returned exactly once."""

    if settings.auth_mode != "required":
        raise ValueError("cannot issue keys while authentication is disabled")
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("credential name is required")
    if expires_at is not None:
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise ValueError("expires_at must be a future timezone-aware timestamp")

    key_id = secrets.token_urlsafe(16)
    secret = secrets.token_urlsafe(32)
    namespace = settings.key_namespace
    pepper_version = settings.active_api_key_pepper_version
    pepper = settings.api_key_pepper(pepper_version)
    if pepper is None:
        raise ValueError("active API-key pepper is unavailable")
    credential = NewCredential(
        credential_id=uuid4(),
        tenant_id=tenant_id,
        key_id=key_id,
        namespace=namespace,
        secret_digest=_digest(pepper, namespace, key_id, secret),
        pepper_version=pepper_version,
        scopes=_normalize_scopes(scopes),
        name=cleaned_name,
        expires_at=expires_at,
    )
    return IssuedApiKey(
        plaintext=f"fg_{namespace}_{key_id}.{secret}",
        credential=credential,
    )


def provision_api_key(
    writer: CredentialWriter,
    *,
    settings: Settings,
    tenant_id: UUID,
    name: str,
    scopes: Iterable[str],
    expires_at: datetime | None = None,
) -> IssuedApiKey:
    issued = issue_api_key(
        settings=settings,
        tenant_id=tenant_id,
        name=name,
        scopes=scopes,
        expires_at=expires_at,
    )
    writer.insert_credential(issued.credential)
    return issued


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid API credential",
        headers={"WWW-Authenticate": "Bearer"},
    )


class ApiKeyVerifier:
    """Resolve a trusted tenant context from a Bearer API key."""

    def __init__(self, settings: Settings, reader: CredentialReader) -> None:
        self._settings = settings
        self._reader = reader
        self._bearer = HTTPBearer(auto_error=False)

    def authenticate(self, raw_key: str) -> TenantContext:
        match = _KEY_PATTERN.fullmatch(raw_key)
        if match is None or match["namespace"] != self._settings.key_namespace:
            raise _unauthorized()

        key_id = match["key_id"]
        record = self._reader.get_by_key_id(key_id)
        version = (
            record.pepper_version
            if record is not None
            else self._settings.active_api_key_pepper_version
        )
        pepper = self._settings.api_key_pepper(version)
        if pepper is None:
            pepper = self._settings.api_key_pepper(
                self._settings.active_api_key_pepper_version
            )
        if pepper is None:
            raise RuntimeError("active API-key pepper is unavailable")

        candidate = _digest(
            pepper,
            match["namespace"],
            key_id,
            match["secret"],
        )
        expected = record.secret_digest if record is not None else _DUMMY_DIGEST
        secret_valid = hmac.compare_digest(candidate, expected)

        now = datetime.now(UTC)
        expires_at = record.expires_at if record is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        unexpired = expires_at is None or expires_at > now
        record_valid = (
            record is not None
            and record.status == "active"
            and record.namespace == self._settings.key_namespace
            and unexpired
        )
        if not (secret_valid and record_valid):
            raise _unauthorized()

        return TenantContext(
            tenant_id=record.tenant_id,
            credential_id=record.credential_id,
            scopes=record.scopes,
        )

    def require(self, *required_scopes: str) -> Callable[..., TenantContext]:
        required = _normalize_scopes(required_scopes)
        bearer = self._bearer
        settings = self._settings

        def dependency(
            credentials: HTTPAuthorizationCredentials | None = Security(bearer),
        ) -> TenantContext:
            if settings.auth_mode == "disabled":
                return TenantContext(
                    tenant_id=settings.disabled_auth_tenant_id,
                    credential_id=settings.disabled_auth_credential_id,
                    scopes=required,
                )
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise _unauthorized()
            context = self.authenticate(credentials.credentials)
            if not required.issubset(context.scopes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="insufficient credential scope",
                )
            return context

        return dependency
