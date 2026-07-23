# FraudGuard AI — High-Level Architecture

Goal: a fraud-detection platform that is **easy to scale**, **cheap to extend**
with new products (B2B API, B2C app, trading-bot guard), and **robust to change**
— i.e. it doesn't silently rot when fraud patterns shift ("works today, not
tomorrow"). Everything here is defensive: we score and explain, we never attack.

---

## 1. The core idea: one engine, many faces

Build **one scoring engine** and expose it through thin product layers. You never
rebuild the brain — you add a new face.

```
                         ┌───────────────────────────┐
   B2B clients  ───────▶ │                           │
   (banks, PSPs, APIs)   │      SCORING ENGINE       │
                         │  (rules + ML + explain)   │ ◀── B2C app (consumer alerts)
   Trading-bot guard ──▶ │                           │ ◀── Internal demo dashboard
                         └───────────────────────────┘
```

The engine takes **transaction metadata** (amount, time, merchant category, device
hash, geo, velocity counters) and returns `{ risk_score, decision, reasons[] }`.
No product layer contains fraud logic of its own — they just call the engine.

---

## 2. Layered design (each layer swappable)

```
┌─────────────────────────────────────────────────────────────┐
│  1. INGESTION      REST /score  ·  batch replay  ·  webhook   │
├─────────────────────────────────────────────────────────────┤
│  2. FEATURE LAYER  feature engineering + feature store        │
│                    (velocity, amount z-score, geo delta …)    │
├─────────────────────────────────────────────────────────────┤
│  3. DECISION LAYER  A) deterministic RULES  →  fast, auditable │
│                     B) ML MODEL (XGBoost)   →  learns patterns │
│                     C) COMBINER + threshold →  final decision  │
├─────────────────────────────────────────────────────────────┤
│  4. EXPLAINABILITY  SHAP / reason codes  →  "why flagged"      │
├─────────────────────────────────────────────────────────────┤
│  5. FEEDBACK/MONITOR  outcomes, drift, retrain trigger        │
└─────────────────────────────────────────────────────────────┘
```

Why layered: you can replace the ML model without touching the API; add a new
rule without retraining; swap the storage without changing product code.

---

## 3. Beating non-determinism ("suits today, not tomorrow")

This is the single most important design decision, so it gets its own section.
An ML model is a **snapshot of yesterday's fraud**. Attackers change tactics, so a
model that's 99% accurate today quietly degrades. We defend against that with
**five mechanisms**, not one:

1. **Split deterministic from probabilistic.** Hard rules (a $0.01 test charge,
   10 tx in 60s, impossible travel) live in the RULES layer — they're stable,
   auditable, and don't drift. The ML model handles the fuzzy "this *feels* off."
   When the model drifts, the rules still hold the floor. Never bet everything on
   the model.

2. **Version *everything*.** Every model is `model@v1.3` with the training data
   snapshot, feature list, and metrics stored alongside it. You can always roll
   back, compare, and prove to an auditor what scored a given transaction.

3. **Monitor drift, don't assume.** Track input drift (are today's transactions
   statistically different from training data?) and outcome drift (is recall
   dropping?). When drift crosses a threshold → alert → retrain. This is the
   `FEEDBACK/MONITOR` layer. A model without monitoring is a liability.

4. **Retraining pipeline, not retraining heroics.** Retraining is one command /
   one scheduled job that pulls fresh labeled outcomes, retrains, evaluates
   against a **held-out recent** slice, and only promotes the new model if it
   beats the old one (champion/challenger). No manual notebook surgery.

5. **Human-in-the-loop feedback.** Analyst decisions (real fraud vs. false alarm)
   flow back as labels. The system gets smarter from being used — a data
   flywheel, and your long-term moat.

> Rule of thumb: **rules give you stability, ML gives you reach, monitoring keeps
> them honest.** Any one alone rots. Together they adapt.

---

## 4. Scalability & expansion

- **Stateless scoring service** → run many copies behind a load balancer; scale
  horizontally with traffic.
- **Feature store** (start: a Postgres table / Redis; later: a real feature store)
  so the *same* feature definitions serve training and live scoring — no
  train/serve skew.
- **Async everything** for batch replays and retraining (a queue, not a blocking
  call).
- **Multi-tenancy from day one** (a `tenant_id` on every request) so one
  deployment serves many banks/merchants — critical for a B2B SaaS.
- **New products = new thin adapters**, not new engines:
  - *B2C app*: calls `/score`, shows the customer a friendly alert.
  - *Trading-bot guard*: calls `/score` on account funding / withdrawal events.
  - *White-label*: same engine, client's branding on the dashboard.

---

## 5. Data & compliance guardrails (bake in from day one)

- Accept **metadata only** — no full card number (PAN) or CVV ever enters the
  system. This keeps you (mostly) out of PCI-DSS scope. See `docs/` for notes.
- All demo/simulator data is **synthetic or public**.
- Encrypt data in transit (TLS) and at rest; log every access; unique IDs per user.
- Align with **Kenya Data Protection Act** + **CBK** fintech guidance locally, and
  **PCI-DSS** for any client that sends real card data (they stay the card-data
  owner; you process tokens/metadata).

---

## 6. Build order (so you always have something demoable)

1. **Baseline model** on the public dataset — prove detection works (PR-AUC,
   recall, false-positive rate).
2. **Explanations** (SHAP reason codes).
3. **`/score` API** (FastAPI) — metadata in, decision + reasons out.
4. **Dashboard** (Streamlit) — replay data live, show catches + ROI.
5. **Monitoring stub** — log scores + outcomes, chart drift.
6. *Then* harden for a real client (multi-tenancy, auth, deploy).

Each step is a thing you can show a bank or investor. That's the "show the
pothole, show the fix" pitch, built incrementally.
