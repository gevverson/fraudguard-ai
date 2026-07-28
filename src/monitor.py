"""
monitor.py — drift monitoring (Pillar B3). The "warning light".

The problem: a model is trained on yesterday's world. As fraud tactics and
customer behaviour shift, today's transactions stop looking like the training
data — and the model quietly gets worse with NO error to alert you. Drift
monitoring measures that shift so you find out early.

We use PSI (Population Stability Index) — the metric banks actually use:
    PSI < 0.10  -> stable   (distributions match; model still valid)
    0.10–0.25   -> warning  (meaningful shift; investigate)
    PSI > 0.25  -> alert    (major shift; retrain likely needed)

PSI compares a REFERENCE distribution (what the model trained on) against a
CURRENT batch (recent live traffic), feature by feature.
"""
import numpy as np
import pandas as pd

EPS = 1e-6


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index for one feature: reference vs current."""
    # Bin edges come from the REFERENCE quantiles (so bins are meaningful)
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf

    ref_pct = np.histogram(reference, bins=edges)[0].astype(float)
    cur_pct = np.histogram(current, bins=edges)[0].astype(float)
    ref_pct = np.clip(ref_pct / ref_pct.sum(), EPS, None)
    cur_pct = np.clip(cur_pct / cur_pct.sum(), EPS, None)

    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def status_of(psi_value: float) -> str:
    if psi_value < 0.10:
        return "stable"
    if psi_value < 0.25:
        return "warning"
    return "alert"


def drift_report(reference: pd.DataFrame, current: pd.DataFrame, features: list):
    """Per-feature PSI + status, plus an overall verdict (worst feature wins)."""
    rows = []
    for f in features:
        val = psi(reference[f].values, current[f].values)
        rows.append({"feature": f, "psi": round(val, 4), "status": status_of(val)})
    report = pd.DataFrame(rows).sort_values("psi", ascending=False).reset_index(drop=True)
    if (report["status"] == "alert").any():
        overall = "alert"
    elif (report["status"] == "warning").any():
        overall = "warning"
    else:
        overall = "stable"
    return report, overall
