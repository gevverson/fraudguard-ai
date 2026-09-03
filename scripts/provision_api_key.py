"""Create a tenant API credential and display its plaintext exactly once."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from uuid import UUID

from src.config import get_settings
from src.persistence import CredentialRepository, Database
from src.security import provision_api_key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    tenant = parser.add_mutually_exclusive_group(required=True)
    tenant.add_argument("--tenant-id", type=UUID)
    tenant.add_argument("--new-tenant-slug")
    parser.add_argument("--tenant-name")
    parser.add_argument("--credential-name", required=True)
    parser.add_argument(
        "--scopes",
        nargs="+",
        default=["decisions:write"],
    )
    parser.add_argument(
        "--expires-at",
        help="Optional timezone-aware ISO-8601 timestamp",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    if settings.auth_mode != "required":
        raise SystemExit("FRAUDGUARD_AUTH_MODE must be required")

    database = Database(settings.database_dsn, auto_create_schema=False)
    try:
        if not database.ready(require_runtime_privileges=False):
            raise SystemExit("database schema is not ready; run alembic upgrade head")
        repository = CredentialRepository(database)
        if args.new_tenant_slug:
            if not args.tenant_name:
                raise SystemExit("--tenant-name is required for a new tenant")
            tenant_id = repository.create_tenant(
                slug=args.new_tenant_slug,
                name=args.tenant_name,
            )
        else:
            tenant_id = args.tenant_id

        expires_at = None
        if args.expires_at:
            expires_at = datetime.fromisoformat(
                args.expires_at.replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                raise SystemExit("--expires-at must include a timezone")
            expires_at = expires_at.astimezone(UTC)

        issued = provision_api_key(
            repository,
            settings=settings,
            tenant_id=tenant_id,
            name=args.credential_name,
            scopes=args.scopes,
            expires_at=expires_at,
        )
        print(f"tenant_id={tenant_id}")
        print(f"credential_id={issued.credential.credential_id}")
        print("Store this API key now; it cannot be recovered:")
        print(issued.plaintext)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
