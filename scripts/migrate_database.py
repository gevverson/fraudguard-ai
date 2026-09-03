"""Apply schema migrations and grant the least-privileged runtime role."""

from __future__ import annotations

import os
import re

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def main() -> None:
    database_url = os.environ.get("FRAUDGUARD_MIGRATION_DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgresql+")):
        raise SystemExit(
            "FRAUDGUARD_MIGRATION_DATABASE_URL must be a PostgreSQL URL"
        )
    runtime_role = os.environ.get("FRAUDGUARD_RUNTIME_DB_ROLE", "")
    if _ROLE_PATTERN.fullmatch(runtime_role) is None:
        raise SystemExit("FRAUDGUARD_RUNTIME_DB_ROLE is required and invalid")
    operator_role = os.environ.get("FRAUDGUARD_OPERATOR_DB_ROLE", "")
    if _ROLE_PATTERN.fullmatch(operator_role) is None:
        raise SystemExit("FRAUDGUARD_OPERATOR_DB_ROLE is required and invalid")

    command.upgrade(Config("alembic.ini"), "head")

    engine = create_engine(database_url)
    quoted_role = engine.dialect.identifier_preparer.quote(runtime_role)
    quoted_operator_role = engine.dialect.identifier_preparer.quote(
        operator_role
    )
    try:
        with engine.begin() as connection:
            database_name = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
            quoted_database = engine.dialect.identifier_preparer.quote(
                database_name
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}"
            )
            connection.exec_driver_sql(
                "GRANT CONNECT ON DATABASE "
                f"{quoted_database} TO {quoted_operator_role}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {quoted_role}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {quoted_operator_role}"
            )
            connection.exec_driver_sql(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted_role}"
            )
            connection.exec_driver_sql(
                "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "
                f"{quoted_operator_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT ON tenants, api_credentials, tenant_policy_versions, "
                "tenant_policy_activations, tenant_policy_activation_events, "
                "alembic_version "
                f"TO {quoted_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE ON decision_records "
                f"TO {quoted_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT ON decision_audit_events "
                f"TO {quoted_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE ON outbox_events "
                f"TO {quoted_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE ON tenants, api_credentials "
                f"TO {quoted_operator_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT ON tenant_policy_versions, "
                "tenant_policy_activation_events "
                f"TO {quoted_operator_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE ON tenant_policy_activations "
                f"TO {quoted_operator_role}"
            )
            connection.exec_driver_sql(
                "GRANT SELECT ON alembic_version "
                f"TO {quoted_operator_role}"
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
