# Tenant Policy Operations

FraudGuard policies are configuration for deterministic defensive rules. They
are not executable code. A policy version is create-only: after creation its
document, SHA-256 digest, tenant, and version cannot be edited or deleted.

## Why this exists

A bank must be able to answer: "Which exact controls produced this decision?"
The answer cannot be "whatever settings were active when we investigated it."
FraudGuard therefore pins `policy_version` while reserving the transaction and
stores it in the response, ledger, audit event, and outbox payload. Retries use
the pinned version even if an operator activates a different policy meanwhile.

Each tenant has:

- immutable policy versions;
- one active pointer with an increasing generation;
- append-only activation history;
- PostgreSQL row-level isolation from every other tenant.

The built-in `rules-v2` policy is created and activated when the tenant is
provisioned. A custom version is never active merely because it was created.

## Policy schema version 1

See [tenant-policy-kes-v1.json](examples/tenant-policy-kes-v1.json).

- `high_amount_review_thresholds` maps an explicitly supported currency to a
  review threshold. There is no implicit currency conversion.
- `velocity_decline_count_60s` is the trusted server-side subject count that
  produces an advisory decline.
- Decimal amounts are represented as strings in policy documents to preserve
  exact values. Payment API amounts remain JSON numbers.

## Safe rollout workflow

Set `TENANT_ID` below only as a shell convenience; it is not accepted from a
payment request. Run policy commands through the database operator role:

```bash
export TENANT_ID="replace-with-tenant-uuid"

docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.manage_policy --tenant-id "$TENANT_ID" show-active
```

Record the displayed generation. Create a new inactive version:

```bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.manage_policy --tenant-id "$TENANT_ID" create \
  --version pilot-kes-v1 \
  --file docs/examples/tenant-policy-kes-v1.json \
  --actor operator:gevverson
```

Review the stored digest and list all versions:

```bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.manage_policy --tenant-id "$TENANT_ID" list
```

Activate only with the generation observed before the change:

```bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.manage_policy --tenant-id "$TENANT_ID" activate \
  --version pilot-kes-v1 \
  --expected-generation 1 \
  --actor operator:gevverson
```

If another operator changed the pointer first, activation fails instead of
overwriting their work. Re-read active state, review the new situation, and
decide whether to try again.

## Rollback

Rollback is another atomic activation; it never edits historical records.
Supply the currently observed generation:

```bash
docker compose --env-file .env.compose --profile ops run --rm operator \
  python -m scripts.manage_policy --tenant-id "$TENANT_ID" rollback \
  --expected-generation 2 \
  --actor operator:gevverson
```

The generation increments again and an append-only event records who restored
the previous version. Existing decisions still identify their original policy.

## What to verify during a pilot

Before activation, replay governed historical metadata offline and compare
approval, review, and decline volumes against the active version. After
activation, monitor those same rates plus latency and dependency failures.
Remember that FraudGuard remains advisory: every response has
`enforced=false`; activation does not authorize automatic payment blocking.
