"""Create, inspect, activate, and roll back immutable tenant policies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from src.config import get_settings
from src.persistence import Database, PolicyRepository
from src.policy import TenantPolicy, policy_document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("show-active")
    subcommands.add_parser("list")

    create = subcommands.add_parser("create")
    create.add_argument("--version", required=True)
    create.add_argument("--file", type=Path, required=True)
    create.add_argument("--actor", required=True)

    activate = subcommands.add_parser("activate")
    activate.add_argument("--version", required=True)
    activate.add_argument("--actor", required=True)
    activate.add_argument("--expected-generation", type=int, required=True)

    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--expected-generation", type=int, required=True)
    return parser.parse_args()


def _print_active(active) -> None:
    print(
        json.dumps(
            {
                "document_sha256": active.snapshot.document_sha256,
                "generation": active.generation,
                "policy": policy_document(active.snapshot.policy),
                "policy_version": active.snapshot.policy_version,
                "tenant_id": str(active.snapshot.tenant_id),
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    args = parse_args()
    settings = get_settings()
    database = Database(settings.database_dsn, auto_create_schema=False)
    try:
        if not database.ready(require_runtime_privileges=False):
            raise SystemExit("database schema is not ready; run migrations first")
        repository = PolicyRepository(database)
        if args.command == "show-active":
            _print_active(repository.resolve_active(args.tenant_id))
        elif args.command == "list":
            active = repository.resolve_active(args.tenant_id)
            versions = repository.list_versions(args.tenant_id)
            print(
                json.dumps(
                    {
                        "active_generation": active.generation,
                        "active_policy_version": active.snapshot.policy_version,
                        "versions": [
                            {
                                "document_sha256": version.document_sha256,
                                "policy_version": version.policy_version,
                            }
                            for version in versions
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "create":
            document = json.loads(args.file.read_text(encoding="utf-8"))
            snapshot = repository.create_version(
                tenant_id=args.tenant_id,
                policy_version=args.version,
                policy=TenantPolicy.model_validate(document),
                actor=args.actor,
            )
            print(
                json.dumps(
                    {
                        "document_sha256": snapshot.document_sha256,
                        "policy_version": snapshot.policy_version,
                        "status": "created_not_active",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "activate":
            _print_active(
                repository.activate(
                    tenant_id=args.tenant_id,
                    policy_version=args.version,
                    actor=args.actor,
                    expected_generation=args.expected_generation,
                )
            )
        elif args.command == "rollback":
            _print_active(
                repository.rollback(
                    tenant_id=args.tenant_id,
                    actor=args.actor,
                    expected_generation=args.expected_generation,
                )
            )
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
