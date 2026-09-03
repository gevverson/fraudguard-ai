#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 --set=database_name="$POSTGRES_DB" --set=migrator_password="$FRAUDGUARD_MIGRATOR_DB_PASSWORD" --set=app_password="$FRAUDGUARD_APP_DB_PASSWORD" --set=operator_password="$FRAUDGUARD_OPERATOR_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE fraudguard_migrator LOGIN PASSWORD %L '
    'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'migrator_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'fraudguard_migrator'
)
\gexec

SELECT format(
    'CREATE ROLE fraudguard_app LOGIN PASSWORD %L '
    'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'fraudguard_app'
)
\gexec

SELECT format(
    'CREATE ROLE fraudguard_operator LOGIN PASSWORD %L '
    'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'operator_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'fraudguard_operator'
)
\gexec

ALTER DATABASE :"database_name" OWNER TO fraudguard_migrator;
ALTER SCHEMA public OWNER TO fraudguard_migrator;
GRANT CONNECT ON DATABASE :"database_name" TO fraudguard_app;
GRANT CONNECT ON DATABASE :"database_name" TO fraudguard_operator;
SQL
