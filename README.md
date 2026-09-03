# FraudGuard AI

FraudGuard AI is an explainable, multi-rail fraud-decisioning platform for
tokenized card, mobile-money, wallet, bank-transfer, and payout metadata.
Phase 1A provides the security and persistence boundary needed to begin a
controlled shadow-pilot integration.

> Status: advisory shadow pilot only. Decisions are never automatically
> enforced. Gateway-grade mTLS/OAuth, quotas, tenant policy rollout, load
> certification, and an analyst workflow remain before live production use.

## What Works

- POST /v1/decisions rejects PAN, CVV/CVC, PIN, track data, unknown fields,
  and caller-supplied velocity.
- Opaque API keys establish immutable tenant identity and scoped authorization.
  Only HMAC digests are stored; keys can be rotated without downtime.
- A PostgreSQL ledger provides tenant-scoped idempotency, exact response replay,
  lease recovery, append-only audit events, and a transactional outbox.
- Redis computes HMAC-keyed 10/60/300-second sliding-window velocity atomically.
  Production configuration fails closed when shared state is unavailable.
- Responses remain rules-only-shadow and enforced=false while versioning every
  schema, policy, feature set, service, and model dependency.
- Liveness, dependency readiness, request IDs, safe structured logs, and
  low-cardinality Prometheus instrumentation are included. The scrape endpoint
  remains local/test-only until an operator-only production boundary exists.
- CI runs unit tests plus real PostgreSQL row-level-security and Redis Lua tests.

## Local Development

Local defaults intentionally use disabled authentication, in-memory SQLite, and
in-memory velocity. They are convenient for development but are rejected by
staging/production configuration.

~~~bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

python -m pytest
uvicorn src.app.api:app --reload --port 8000
~~~

Open http://localhost:8000/docs. See [Pilot Operations](docs/PILOT_OPERATIONS.md)
for authenticated PostgreSQL/Redis startup, migrations, and provisioning.

## Decision Request

~~~bash
curl -X POST http://localhost:8000/v1/decisions \
  -H "Authorization: Bearer fg_live_<key-id>.<secret>" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: checkout-01J8Z6K4W4" \
  -d '{
    "transaction_id": "txn_01J8Z6K4W4",
    "occurred_at": "2026-09-01T09:30:00+03:00",
    "amount": 125.50,
    "currency": "KES",
    "channel": "mobile_money",
    "transaction_type": "transfer",
    "identifiers": {
      "customer_id": "cus_01J8Z6K4W4",
      "account_id": "acct_01J8Z6K4W4"
    }
  }'
~~~

Retry the same semantic request with the same transaction_id to receive the
byte-identical stored response. Reusing that ID with different data returns
409 idempotency_conflict.

## Model Evaluation

Place the public MLG-ULB dataset at data/creditcard.csv, then run
venv/bin/python src/models/train.py. The untouched future slice reports PR-AUC
0.7894, recall 77.03%, precision 59.38%, false-positive rate 0.0688%, and
value-weighted recall 65.89%. Evidence lives in reports/metrics.json; generated
models remain untracked.

## Repository Layout

- src/config.py, security.py, persistence.py, velocity.py, service.py — the
  authenticated shadow-pilot core
- src/contracts.py, decision.py, rules.py — versioned public semantics
- src/app/ — FastAPI service and Streamlit demonstration
- migrations/ and scripts/ — schema and operator workflows
- tests/ — unit/API tests plus PostgreSQL and Redis integrations
- docs/, reports/, models/registry/ — design, evidence, and metadata

Tokenization reduces exposure but does not independently remove PCI DSS,
Kenyan data-protection, or payment-services obligations. See
[Roadmap](docs/ROADMAP.md) and [Architecture](docs/ARCHITECTURE.md).
