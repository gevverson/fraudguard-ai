# FraudGuard AI Architecture

FraudGuard is currently a tenant-aware advisory decision service. It never
enforces a payment action and never accepts raw payment credentials.

## Online Decision Path

~~~text
Client / API gateway
        |
        v
FastAPI request boundary
  request ID · streamed size limit · API-key scope · strict schema
        |
        v
Redis tenant quota
  HMAC-only tenant key · atomic token bucket
        |
        v
PostgreSQL reservation
  tenant + transaction id · request HMAC · processing lease
        |
        v
Redis velocity                 Versioned rules
  HMAC-only keys  -----------> static + trusted velocity
        |                              |
        +------------------------------+
                       |
                       v
PostgreSQL completion transaction
  exact response · provenance · audit event · outbox event
                       |
                       v
Advisory response (enforced=false)
~~~

The response is returned only after the completion transaction commits. A
retry with the same semantic request replays the stored bytes; a changed
request under the same tenant/transaction key conflicts. An active lease
prevents concurrent duplicate effects, and expired or retryable work can be
reclaimed.

## Trust and Tenant Boundaries

Tenant identity comes only from a verified Bearer API key. Payload fields,
merchant_id, query parameters, and X-Tenant-ID never establish tenancy.
Credential records contain a lookup identifier and HMAC digest, not the raw
secret. The PostgreSQL runtime role is non-superuser and cannot bypass
row-level security. Every tenant-owned query also carries an explicit tenant
predicate and sets transaction-local fraudguard.tenant_id.

Opaque payment identifiers are HMACed before entering the ledger or Redis.
Transaction IDs remain available for idempotency and support, so they must also
be opaque. Request bodies and authorization headers are excluded from logs and
metric labels.

## Redis State and Failure Semantics

Redis Lua uses server time, sorted sets, ZADD NX, exact open-left/closed-right
windows, and TTLs. Duplicate requests do not increment or refresh an event.
Counts are ignored while state is warming. Staging and production require Redis
and fail closed with 503; local/test may explicitly use degraded shadow mode.

Tenant quotas use an independent Redis token bucket keyed only by an HMAC of
the authenticated tenant. Redis server time and Lua make checks atomic across
API replicas. Quota exhaustion returns 429 with bounded limit, remaining, and
retry headers; backend failure returns 503. Streamed request bytes are counted
even when Content-Length is absent or false.

PostgreSQL is equally mandatory outside local/test. If commit outcome is
uncertain, no recommendation is returned; a retry resolves the durable state.
/health/live never calls dependencies, while /health/ready checks the ledger
schema and velocity backend.

## Deployment Boundary

Alembic owns schema changes. The container stack separates administrator,
migration-owner, and runtime roles; the API role receives only required DML
privileges. Prometheus metrics use bounded labels. The next boundary is an
external gateway with mTLS or private-key JWT,
OpenTelemetry export, a managed outbox publisher, and certified load tests.
