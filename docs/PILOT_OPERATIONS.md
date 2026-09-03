# Shadow Pilot Operations

This runbook is for an advisory pilot. It is not authorization to process raw
cardholder data or automatically block payments.

## Bootstrap

1. Copy `.env.example` to `.env.compose` and replace every password and
   base64url pepper. API-key and data peppers must be independent and at least
   256 bits. Keep this file local and permission it `0600`.
2. Do not source `.env.compose` in a shell: its JSON pepper map must retain its
   quotes. Pass it explicitly to Compose instead. This also prevents staging
   values from leaking into local unit tests.
3. Start the stack with
   `docker compose --env-file .env.compose up -d --build`. The one-shot migrate
   service applies Alembic migrations and grants the non-bypass runtime role.
4. Confirm GET /health/live returns 200 and GET /health/ready reports both
   PostgreSQL and Redis ready.
5. Provision a tenant credential:

~~~bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.provision_api_key \
  --new-tenant-slug design-partner \
  --tenant-name "Design Partner" \
  --credential-name primary
~~~

Store the returned key in the client's secret manager. It is displayed once.
The tenant starts on the immutable `rules-v2` policy. Follow
[Tenant Policy Operations](TENANT_POLICIES.md) to create an inactive version,
review its digest, activate it with an expected generation, or roll back.
Create a second credential before rotating clients, then revoke the old one:

~~~bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.revoke_api_key \
  --credential-id 00000000-0000-0000-0000-000000000000
~~~

## Operational Signals

- /health/live checks only the process; use it for restart decisions.
- /health/ready checks the ledger schema and velocity dependency; remove an
  instance from traffic when it returns 503.
- Metrics instrumentation is present, but `/metrics` is mounted only in local
  and test environments. Do not grant design-partner credentials access to
  process-wide metrics; add an operator-only collector boundary before exposing
  them in staging or production.
- Correlate support cases with X-Request-ID; never log authorization headers,
  bodies, tokens, tenant IDs, or transaction IDs.
- Decision quotas are per authenticated tenant. Successful decisions return
  X-RateLimit-Limit and X-RateLimit-Remaining; 429 responses also return
  Retry-After. Keep these limits below the managed gateway ceiling.

## Failure Contract

- 401: missing, malformed, unknown, expired, or revoked credential.
- 403: valid credential without the required scope.
- 429 rate_limit_exceeded: retry after the supplied Retry-After.
- 409 idempotency_conflict: transaction ID reused with different data.
- 409 decision_in_progress: retry after the supplied Retry-After.
- 503: Redis, the pinned tenant policy, or the durable ledger is unavailable;
  do not act on a decision.

Staging/production does not silently fall back to local memory or SQLite.

## Before a Paid Pilot

Terminate TLS and require mTLS or private-key JWT at a managed gateway.
Configure managed PostgreSQL/Redis with
backups and HA, export traces, deploy an outbox worker, exercise restore and key
rotation, and complete the Phase 1B load/security gates. Keep every integration
non-enforcing until contractual, privacy, regulatory, and operational reviews
are complete.
