# FraudGuard AI Roadmap

This roadmap separates a credible demonstration from a system that may safely
support commercial payment decisions. A milestone is complete only when its
code, tests, evidence, and documentation are committed.

## Demonstration Foundation — Complete

- [x] Public-dataset exploration and class-imbalance analysis
- [x] XGBoost baseline with PR-AUC, recall, precision, and cost-oriented views
- [x] SHAP explanations and a Streamlit demonstration dashboard
- [x] Deterministic rules floor, local model registry, and PSI demonstration
- [x] Legacy PCA-based `/score` endpoint

## Phase 0: Trustworthy Foundation — Complete

- [x] Canonical `POST /v1/decisions` contract for tokenized multi-rail events
- [x] Sensitive-field rejection and strict request validation
- [x] Versioned, non-enforcing `rules-only-shadow` decision semantics
- [x] Currency-safe canonical rules; no caller-supplied velocity context
- [x] Lazy, deprecated legacy-model loading
- [x] Chronological train/calibration/test isolation
- [x] Platt calibration and calibration-only operating-threshold selection
- [x] Untouched-test metrics, bootstrap confidence intervals, dataset/model hashes
- [x] Pinned dependencies, Ruff, automated tests, coverage gate, and CI
- [x] Generated models moved out of `src/` and ignored by Git

## Phase 1: Shadow Pilot Platform — Next

- [ ] API gateway plus OAuth private-key JWT or mTLS authentication
- [ ] Trusted tenant identity, quotas, request-size limits, and rate limiting
- [ ] PostgreSQL decision ledger with tenant isolation and row-level security
- [ ] Idempotency on `(tenant_id, transaction_id)`
- [ ] Redis-backed server-side velocity and feature state
- [ ] Versioned tenant policies and atomic configuration rollout
- [ ] Transactional outbox, structured audit events, and OpenTelemetry
- [ ] Container deployment, readiness checks, secrets management, and load tests

Exit gate: cross-tenant tests pass; duplicate requests have no duplicate side
effects; every decision records model/policy/feature versions; no prohibited
payment credentials enter payloads or logs; p95 latency is at most 100 ms at
twice the expected pilot peak.

## Phase 2: Controlled Learning

- [ ] Analyst queue, human overrides, and dispute/chargeback label ingestion
- [ ] Point-in-time-correct offline/online feature pipeline
- [ ] Managed model registry with human-approved champion/challenger promotion
- [ ] Shadow and canary rollout with rollback under five minutes
- [ ] System, data, model, calibration, fraud-loss, and review-volume monitoring
- [ ] Thirty-day customer comparison against the incumbent controls

## Phase 3: General Availability

- [ ] Independent penetration test and remediation
- [ ] Backup/restore and disaster-recovery exercises against contracted RPO/RTO
- [ ] Retention, deletion, incident response, subprocessor, and audit workflows
- [ ] SLA, on-call operations, billing, quotas, and optional dedicated tenancy
- [ ] Kenyan legal/CBK scope opinion, ODPC/DPIA completion, and PCI QSA scoping

## Commercial Validation in Parallel

Interview 30 Kenyan PSP, merchant, bank/SACCO, and integration prospects. Advance
only after three design partners agree to provide anonymized historical data and
pay for a shadow pilot. Measure fraud-value recall, false-positive rate, review
volume, approval impact, and latency rather than headline accuracy.
