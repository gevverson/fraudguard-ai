# FraudGuard AI — Roadmap (our living checklist)

The rule: we tick a box ONLY when it's built, run, and pushed. This file is the
single source of truth for "what have we done / what's next." Update it every
session.

The product must work **today, tomorrow, and for life** — so the roadmap is split
into three pillars, not one. Pillar B (below) is the non-determinism defense: the
part that keeps the system correct as fraud patterns change. It is the pillar we
had NOT started, and it's next.

---

## Pillar A — Works TODAY (the engine + the demo)  ✅ complete

- [x] A1 · Understand the data (EDA, imbalance, amount/time patterns)
- [x] A2 · Baseline ML model — XGBoost, class-weighted (80% recall, 0.82 PR-AUC)
- [x] A3 · The dial — three-zone approve / review / decline
- [x] A4 · Explainability — SHAP global + per-transaction reasons
- [x] A5 · `/score` API (FastAPI, metadata-only)
- [x] A6 · Demo dashboard (problem, dial, action queue, score-one, live scan)

## Pillar B — Works TOMORROW & FOR LIFE (beat non-determinism)  ⏳ NOT STARTED

> This is the part that makes the product survive. Build order matters: the rules
> floor first (so we're never naked when the model drifts), then the ability to
> see drift, then the ability to fix it.

- [x] B1 · **Deterministic RULES layer** — `src/rules.py`: extensible rule engine
      (one function per rule) + `combine()` that takes the STRICTER of ML vs rules.
      Live rules: $0 auth, high-amount. Velocity, card-testing-burst & impossible-
      travel coded but inert until per-card context arrives. (Refined: a *single*
      micro-charge no longer flags — only a burst — to avoid flooding the queue.)
      Proven in `src/rules_demo.py`. WIRED into `/score` API and the dashboard —
      responses now show ml_decision, final decision, and which rules fired.
- [x] B2 · **Model registry + versioning** — `src/registry.py` + `src/register_model.py`:
      every model saved as `v1, v2, …` with its data fingerprint, params, metrics,
      and a sha256, indexed in `models/registry/registry.json` (committed = audit
      trail; binaries git-ignored). Champion pointer + one-call `set_champion()`
      rollback. API `/health` + `/score` and the dashboard header now report the
      serving version. Demonstrated: registered v1 & v2, rolled back to v1.
- [x] B3 · **Drift monitoring** — `src/monitor.py`: PSI (Population Stability Index)
      of live traffic vs the training reference, per feature, with stable/warning/
      alert thresholds (0.10 / 0.25). Proven in `src/monitor_demo.py` (stable→no
      false alarm; injected shift→alert on the right features). Wired into the
      dashboard as section 6 "Model health" with a live warning light + a toggle
      to simulate a shift. (Outcome drift — tracking recall over time — is a B4/B5
      follow-on once real labels flow back.)
- [ ] B4 · **Retraining pipeline** — one command: pull fresh labels, retrain,
      evaluate on a recent held-out slice, promote ONLY if it beats the champion
      (champion / challenger).
- [ ] B5 · **Human-in-the-loop feedback** — analyst decisions (real fraud vs false
      alarm) flow back as labels. The system improves from being used.

## Pillar C — Works AT SCALE (productionize + sell)  ⏳ later

- [ ] C1 · Wire the dashboard to call the `/score` API (not the model directly)
- [ ] C2 · API auth + multi-tenancy (`tenant_id` per request)
- [ ] C3 · Deploy (shareable demo link)
- [ ] C4 · Pitch deck / business one-pager
- [ ] C5 · PSP-platform integration plan (call `/score` on MTN flows)

---

## Where we are right now

Pillar A is done. **Pillar B is next, starting with B1 (the rules layer)** —
because a rules floor is what protects you the day the model silently goes stale.
That is the direct answer to "build something that fits today, tomorrow, and for
life."
