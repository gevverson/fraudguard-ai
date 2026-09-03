# FraudGuard AI Roadmap

A milestone is complete only when its code, tests, evidence, and documentation
are committed. Shadow means recommendations are measured but never enforced.

## Demonstration Foundation — Complete

- [x] Public-dataset analysis, XGBoost baseline, SHAP explanations, dashboard
- [x] Deterministic rules, local model registry, PSI demonstration
- [x] Legacy PCA-based /score demonstration

## Phase 0: Trustworthy Foundation — Complete

- [x] Strict tokenized POST /v1/decisions contract
- [x] Sensitive-field rejection and currency-safe shadow rules
- [x] Chronological train/calibration/test isolation and Platt calibration
- [x] Untouched-test metrics, bootstrap intervals, and reproducibility hashes
- [x] Pinned dependencies, tests, branch coverage, linting, and CI

## Phase 1A: Shadow-Pilot Core — Complete

- [x] HMAC-digested scoped API keys and trusted tenant context
- [x] Production fail-closed configuration and request-size boundary
- [x] PostgreSQL ledger with forced row-level security
- [x] Lease-based idempotency on (tenant_id, transaction_id)
- [x] Exact response replay, structured audit events, and transactional outbox
- [x] Redis velocity with atomic sliding windows and HMAC-only keys
- [x] Versioned response, policy, feature, model, and service provenance
- [x] Liveness/readiness, request IDs, safe logs, and Prometheus metrics
- [x] Alembic migrations and separated migration/runtime database roles
- [x] Real PostgreSQL RLS and Redis concurrency tests in CI

## Phase 1B: Pilot Operations — In Progress

- [ ] Put the API behind mTLS or OAuth private-key JWT at a managed gateway
- [x] Add tenant quotas, distributed rate limiting, and streamed body limits
- [x] Add versioned tenant policies with atomic rollout and rollback
- [ ] Publish outbox events with retries, dead-letter handling, and lag alarms
- [ ] Export OpenTelemetry traces without identifiers or request bodies
- [ ] Add managed secrets, backup/restore, Redis HA, and deployment manifests
- [ ] Load-test at twice pilot peak and prove p95 latency at most 100 ms
- [ ] Complete threat modeling, dependency scanning, and penetration-test scope

Exit gate: real cross-tenant tests pass; duplicates have no duplicate effects;
every response is durably attributable; prohibited credentials enter neither
payloads nor logs; recovery and latency targets are demonstrated.

## Phase 2: Controlled Learning

- [ ] Analyst queue, human overrides, and dispute/chargeback label ingestion
- [ ] Point-in-time-correct offline/online features
- [ ] Human-approved champion/challenger promotion with canary rollback
- [ ] Data, calibration, fraud-loss, approval, and review-volume monitoring
- [ ] Thirty-day comparison against each design partner's incumbent controls

## Phase 3: General Availability

- [ ] Independent penetration test and remediation
- [ ] Disaster-recovery exercises against contracted RPO/RTO
- [ ] Retention, deletion, incident, subprocessor, and audit workflows
- [ ] SLA, on-call operations, billing, quotas, and dedicated-tenancy option
- [ ] Kenyan legal/CBK scope opinion, ODPC/DPIA, and PCI QSA scoping

## Commercial Validation — Run in Parallel

Interview 30 Kenyan PSP, merchant, bank/SACCO, and integration prospects. Advance
after three design partners agree to provide properly governed historical data
and pay for a shadow pilot. Measure fraud-value recall, false-positive rate,
review volume, approval impact, and latency—not headline accuracy.
