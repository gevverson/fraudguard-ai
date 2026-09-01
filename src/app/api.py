"""FraudGuard AI HTTP API.

The canonical endpoint accepts tokenized payment metadata and produces a
non-enforcing, rules-only recommendation. Raw PAN, CVV, PIN, and track data are
not accepted. Tokenization can reduce exposure, but PCI DSS scope depends on the
complete deployment architecture and must be assessed separately.

The PCA-based /score demo remains for compatibility. Its model and explanation
dependencies load only when that deprecated endpoint is called.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, NamedTuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import registry, rules
from ..contracts import DecisionRequest, DecisionResponse, RuleHitResponse
from ..decision import (
    ENGINE_MODE,
    LEGACY_DECLINE_AT,
    LEGACY_REVIEW_AT,
    POLICY_VERSION,
    decide_payment,
    legacy_action_for_probability,
)

FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]
REVIEW_AT = LEGACY_REVIEW_AT
DECLINE_AT = LEGACY_DECLINE_AT

app = FastAPI(
    title="FraudGuard AI",
    description=(
        "Advisory fraud decisioning for tokenized payment metadata. "
        "Do not send PAN, CVV, PIN, or magnetic-stripe track data."
    ),
    version="0.2.0",
)


class Transaction(BaseModel):
    """Deprecated PCA demo request retained for POST /score clients."""

    model_config = ConfigDict(extra="forbid")

    features: Dict[str, float] = Field(
        ..., description="Legacy keys: Time, V1..V28, Amount"
    )
    context: Dict[str, float] = Field(
        default_factory=dict,
        description="Deprecated caller-supplied context used only by the legacy demo",
    )


class LegacyRuntime(NamedTuple):
    model: Any
    explainer: Any | None
    model_version: str


class LegacyModelUnavailable(RuntimeError):
    """Raised when no local artifact can serve the deprecated endpoint."""


@lru_cache(maxsize=1)
def _load_legacy_runtime() -> LegacyRuntime:
    """Load legacy artifacts on first /score call, never during import or health."""

    try:
        model = registry.load()
        model_version = registry.get_champion() or "registry-unversioned"
    except Exception:
        try:
            import joblib

            model = joblib.load("models/baseline_xgb.joblib")
            model_version = "unversioned"
        except Exception as fallback_error:
            raise LegacyModelUnavailable(
                "legacy model artifact is not installed"
            ) from fallback_error
    try:
        import shap

        explainer = shap.TreeExplainer(model)
    except Exception:
        explainer = None
    return LegacyRuntime(model, explainer, model_version)


def _registered_model_version() -> str | None:
    try:
        return registry.get_champion()
    except Exception:
        return None


@app.get("/health")
def health():
    return {
        "status": "ok",
        "canonical_engine": ENGINE_MODE,
        "policy_version": POLICY_VERSION,
        "model_version": _registered_model_version(),
        "features_expected": len(FEATURES),
        "legacy_model_loaded": _load_legacy_runtime.cache_info().currsize > 0,
    }


@app.post("/v1/decisions", response_model=DecisionResponse)
def create_decision(payment: DecisionRequest) -> DecisionResponse:
    """Return a deterministic recommendation without enforcing a payment action."""

    outcome = decide_payment(
        payment.transaction_id, payment.model_dump(mode="python")
    )
    return DecisionResponse(
        transaction_id=outcome.transaction_id,
        decision=outcome.decision,
        rule_hits=[
            RuleHitResponse(
                rule=hit.rule,
                action=hit.action,
                reason=hit.reason,
            )
            for hit in outcome.rule_hits
        ],
    )


@app.post(
    "/score",
    deprecated=True,
    summary="Deprecated PCA demo scorer",
    description="Compatibility endpoint; new integrations must use POST /v1/decisions.",
)
def score(tx: Transaction):
    missing = [feature for feature in FEATURES if feature not in tx.features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing features: {missing}")
    try:
        runtime = _load_legacy_runtime()
    except LegacyModelUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Legacy scoring model is unavailable; use /v1/decisions "
                "or install an artifact"
            ),
        ) from exc

    import pandas as pd

    row = pd.DataFrame(
        [[tx.features[feature] for feature in FEATURES]],
        columns=FEATURES,
    )
    probability = float(runtime.model.predict_proba(row)[0, 1])
    ml_decision = legacy_action_for_probability(probability).value
    decision, hits = rules.combine(ml_decision, tx.features, tx.context)
    rule_hits = [
        {"rule": hit.rule, "action": hit.action, "reason": hit.reason}
        for hit in hits
    ]

    reasons = []
    if runtime.explainer is not None:
        shap_values = runtime.explainer.shap_values(row)[0]
        ranked = sorted(
            zip(FEATURES, shap_values),
            key=lambda item: abs(item[1]),
            reverse=True,
        )
        reasons = [
            {
                "feature": feature,
                "pushed_toward": "fraud" if value > 0 else "legit",
                "weight": round(float(value), 3),
            }
            for feature, value in ranked[:4]
        ]
    return {
        "model_version": runtime.model_version,
        "fraud_probability": round(probability, 4),
        "risk_score": round(probability * 100, 1),
        "ml_decision": ml_decision,
        "decision": decision,
        "rule_hits": rule_hits,
        "reasons": reasons,
    }
