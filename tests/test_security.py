import base64
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from scripts.provision_api_key import parse_args
from src.config import Settings
from src.security import (
    ApiKeyVerifier,
    CredentialRecord,
    issue_api_key,
    provision_api_key,
)


def encoded_secret(character=b"k"):
    return base64.urlsafe_b64encode(character * 32).decode().rstrip("=")


def auth_settings(**overrides):
    values = {
        "environment": "test",
        "auth_mode": "required",
        "api_key_peppers": {"v1": encoded_secret()},
        "data_pepper": encoded_secret(b"d"),
    }
    values.update(overrides)
    return Settings(**values)


class MemoryCredentialStore:
    def __init__(self):
        self.records = {}

    def insert_credential(self, credential):
        self.records[credential.key_id] = CredentialRecord(
            credential_id=credential.credential_id,
            tenant_id=credential.tenant_id,
            key_id=credential.key_id,
            namespace=credential.namespace,
            secret_digest=credential.secret_digest,
            pepper_version=credential.pepper_version,
            scopes=credential.scopes,
            status="active",
            expires_at=credential.expires_at,
        )

    def get_by_key_id(self, key_id):
        return self.records.get(key_id)


def test_issued_key_is_opaque_and_plaintext_is_not_persistable_record_data():
    issued = issue_api_key(
        settings=auth_settings(),
        tenant_id=uuid4(),
        name="pilot",
        scopes=["decisions:write"],
    )
    assert issued.plaintext.startswith("fg_test_")
    assert issued.plaintext not in repr(issued.credential)
    assert len(issued.credential.secret_digest) == 32
    assert issued.credential.scopes == frozenset({"decisions:write"})


def test_provisioned_key_authenticates_to_the_stored_tenant():
    settings = auth_settings()
    store = MemoryCredentialStore()
    tenant_id = uuid4()
    issued = provision_api_key(
        store,
        settings=settings,
        tenant_id=tenant_id,
        name="pilot",
        scopes=["decisions:write", "metrics:read"],
    )
    context = ApiKeyVerifier(settings, store).authenticate(issued.plaintext)
    assert context.tenant_id == tenant_id
    assert context.credential_id == issued.credential.credential_id
    assert context.scopes == frozenset({"decisions:write", "metrics:read"})


@pytest.mark.parametrize(
    "mutator",
    [
        lambda key: "not-a-key",
        lambda key: key.replace("fg_test_", "fg_live_", 1),
        lambda key: key[:-1] + ("a" if key[-1] != "a" else "b"),
    ],
)
def test_malformed_namespace_or_wrong_secret_has_generic_401(mutator):
    settings = auth_settings()
    store = MemoryCredentialStore()
    issued = provision_api_key(
        store,
        settings=settings,
        tenant_id=uuid4(),
        name="pilot",
        scopes=["decisions:write"],
    )
    with pytest.raises(HTTPException) as error:
        ApiKeyVerifier(settings, store).authenticate(mutator(issued.plaintext))
    assert error.value.status_code == 401
    assert error.value.detail == "invalid API credential"
    assert error.value.headers == {"WWW-Authenticate": "Bearer"}


def test_unknown_revoked_and_expired_credentials_fail_identically():
    settings = auth_settings()
    store = MemoryCredentialStore()
    issued = provision_api_key(
        store,
        settings=settings,
        tenant_id=uuid4(),
        name="pilot",
        scopes=["decisions:write"],
    )
    verifier = ApiKeyVerifier(settings, store)

    unknown = issued.plaintext.replace(
        issued.credential.key_id, "A" * len(issued.credential.key_id)
    )
    with pytest.raises(HTTPException, match="invalid API credential"):
        verifier.authenticate(unknown)

    record = store.records[issued.credential.key_id]
    store.records[record.key_id] = replace(record, status="revoked")
    with pytest.raises(HTTPException, match="invalid API credential"):
        verifier.authenticate(issued.plaintext)

    store.records[record.key_id] = replace(
        record,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(HTTPException, match="invalid API credential"):
        verifier.authenticate(issued.plaintext)


def test_key_issuance_validates_name_scopes_and_expiry():
    settings = auth_settings()
    with pytest.raises(ValueError, match="name"):
        issue_api_key(
            settings=settings,
            tenant_id=uuid4(),
            name=" ",
            scopes=["decisions:write"],
        )
    with pytest.raises(ValueError, match="scopes"):
        issue_api_key(
            settings=settings,
            tenant_id=uuid4(),
            name="pilot",
            scopes=["INVALID SCOPE"],
        )
    with pytest.raises(ValueError, match="future"):
        issue_api_key(
            settings=settings,
            tenant_id=uuid4(),
            name="pilot",
            scopes=["decisions:write"],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_key_issuance_is_disabled_when_authentication_is_disabled():
    with pytest.raises(ValueError, match="authentication is disabled"):
        issue_api_key(
            settings=Settings(environment="test", auth_mode="disabled"),
            tenant_id=uuid4(),
            name="pilot",
            scopes=["decisions:write"],
        )


def test_provisioning_defaults_do_not_grant_global_metrics_access(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision_api_key.py",
            "--tenant-id",
            str(uuid4()),
            "--credential-name",
            "primary",
        ],
    )
    assert parse_args().scopes == ["decisions:write"]
