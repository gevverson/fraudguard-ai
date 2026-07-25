"""
api.py — FraudGuard AI scoring service (FastAPI).

This turns the model into a PRODUCT: other software sends a transaction's
metadata, and gets back {risk_score, decision, reasons}. No card number (PAN) or
CVV is ever accepted — metadata only — which keeps us out of PCI-DSS scope.

The DECISION uses the three-zone dial you learned:
    approve  <  REVIEW_AT  <=  review  <  DECLINE_AT  <=  decline
Move these two numbers to re-tune the product per client. No retraining needed.

Run the server:
    venv/bin/uvicorn src.app.api:app --reload --port 8000
Then open http://localhost:8000/docs for an auto-generated test page.
"""
from typing import Dict
import joblib
import pandas as pd
import shap
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import rules      # the deterministic floor (B1)
from .. import registry   # model versioning (B2)

# Column order the model expects
FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]

# The dial (tunable per client — this is your product knob, not a code change)
REVIEW_AT = 0.30      # below this: auto-approve
DECLINE_AT = 0.80     # at/above this: auto-decline; between the two: send to review

app = FastAPI(
    title="FraudGuard AI",
    description="Defensive real-time card-fraud scoring. Metadata only — no PAN/CVV.",
    version="0.1.0",
)

# Load the CHAMPION model from the registry (fallback to the legacy file)
try:
    model = registry.load()
    MODEL_VERSION = registry.get_champion()
except Exception:
    model = joblib.load("models/baseline_xgb.joblib")
    MODEL_VERSION = "unversioned"
explainer = shap.TreeExplainer(model)


class Transaction(BaseModel):
    """A transaction's metadata. 'features' must contain all 30 model inputs.
    'context' optionally carries per-card history (e.g. tx_count_last_60s) that
    lets the velocity rules fire — omit it and those rules stay inert."""
    features: Dict[str, float] = Field(
        ..., description="Keys: Time, V1..V28, Amount"
    )
    context: Dict[str, float] = Field(
        default_factory=dict, description="Optional per-card history for velocity rules"
    )


@app.get("/health")
def health():
    return {"status": "ok", "model_version": MODEL_VERSION,
            "features_expected": len(FEATURES)}


@app.post("/score")
def score(tx: Transaction):
    # 1. Validate we got every feature the model needs
    missing = [f for f in FEATURES if f not in tx.features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing features: {missing}")

    # 2. Build a one-row frame in the exact column order and score it
    row = pd.DataFrame([[tx.features[f] for f in FEATURES]], columns=FEATURES)
    proba = float(model.predict_proba(row)[0, 1])

    # 3. ML decision via the three-zone dial
    if proba >= DECLINE_AT:
        ml_decision = "decline"
    elif proba >= REVIEW_AT:
        ml_decision = "review"      # e.g. trigger an OTP / manual check
    else:
        ml_decision = "approve"

    # 3b. Apply the deterministic RULES floor — final = stricter of (ML, rules)
    decision, hits = rules.combine(ml_decision, tx.features, tx.context)
    rule_hits = [{"rule": h.rule, "action": h.action, "reason": h.reason} for h in hits]

    # 4. Explain it — top features that moved this decision (SHAP)
    sv = explainer.shap_values(row)[0]
    ranked = sorted(zip(FEATURES, sv), key=lambda kv: abs(kv[1]), reverse=True)
    reasons = [
        {
            "feature": f,
            "pushed_toward": "fraud" if v > 0 else "legit",
            "weight": round(float(v), 3),
        }
        for f, v in ranked[:4]
    ]

    return {
        "model_version": MODEL_VERSION,          # which model scored this (audit)
        "fraud_probability": round(proba, 4),
        "risk_score": round(proba * 100, 1),     # friendly 0-100
        "ml_decision": ml_decision,
        "decision": decision,                    # final = stricter of ML and rules
        "rule_hits": rule_hits,                  # which rules fired (the floor)
        "reasons": reasons,
    }
