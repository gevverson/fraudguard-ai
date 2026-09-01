# FraudGuard AI

FraudGuard AI is an explainable fraud-decisioning prototype for card, mobile
money, wallet, bank-transfer, and payout metadata. Phase 0 provides a strict
integration contract and a non-enforcing rules engine while preserving the
original PCA-model demo.

> Status: suitable for demonstrations, evaluation, and shadow pilots only. It
> does not yet provide authentication, tenant isolation, durable audit storage,
> trusted velocity state, or automated payment enforcement.

## What Works

- `POST /v1/decisions` accepts a canonical payment event with timezone-aware
  timestamps, ISO-style currency codes, and opaque tokenized identifiers.
- Raw PAN, CVV/CVC, PIN, and track-data fields are rejected.
- Responses are deterministic, versioned, and explicitly marked
  `rules-only-shadow` with `enforced: false` and no invented ML probability.
- The deprecated `POST /score` endpoint keeps the public PCA demo working and
  loads its local model only when called.
- Training uses chronological train/calibration/test slices. Calibration and
  threshold selection occur before the untouched test set is evaluated.
- CI runs syntax checks, Ruff, 50 automated tests, and an 85% branch-coverage
  gate for the new decision and evaluation core.

## Quickstart

Run all commands from the repository root:

~~~bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

python -m pytest
uvicorn src.app.api:app --reload --port 8000
~~~

Open `http://localhost:8000/docs` for the interactive API specification.

## Canonical Decision Example

~~~bash
curl -X POST http://localhost:8000/v1/decisions \
  -H "Content-Type: application/json" \
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

The endpoint returns an advisory `approve`, `review`, or `decline`
recommendation plus the policy version and any deterministic rule hits.

## Model Evaluation

Place the public MLG-ULB dataset at `data/creditcard.csv`, then run:

~~~bash
venv/bin/python src/models/train.py
~~~

The current untouched future/test slice reports PR-AUC `0.7894`, recall
`77.03%`, precision `59.38%`, false-positive rate `0.0688%`, and
value-weighted recall `65.89%`. Confidence intervals and reproducibility
metadata are stored in `reports/metrics.json`. Generated model and policy
artifacts stay untracked under `models/`.

## Repository Layout

- `src/contracts.py` — strict public request/response schemas
- `src/decision.py` and `src/rules.py` — versioned decision semantics
- `src/app/` — FastAPI service and Streamlit demo
- `src/models/` — training, calibration, evaluation, and explainability
- `tests/` — dataset-independent API, rules, and evaluation tests
- `reports/` — committed metrics and figures
- `docs/` — architecture, learning notes, and delivery roadmap

## Security Boundary

Use only public, synthetic, anonymized, or properly tokenized data. Never send
or commit PANs, CVVs, PIN blocks, credentials, or real customer datasets.
Tokenized metadata reduces exposure but does not by itself remove PCI DSS,
Kenyan data-protection, or payment-services obligations.

See `LICENSE` and `docs/ROADMAP.md`.
