"""Revoke one API credential by its non-secret identifier."""

from __future__ import annotations

import argparse
from uuid import UUID

from src.config import get_settings
from src.persistence import CredentialRepository, Database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-id", required=True, type=UUID)
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings.database_dsn, auto_create_schema=False)
    try:
        if not database.ready(require_runtime_privileges=False):
            raise SystemExit("database schema is not ready")
        repository = CredentialRepository(database)
        if not repository.revoke(args.credential_id):
            raise SystemExit("credential was not found")
        print(f"revoked credential_id={args.credential_id}")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
