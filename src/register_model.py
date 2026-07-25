"""
register_model.py — train a model and register it as a version (B2).

This replaces "train and dump to one file". Every run produces an auditable,
versioned model with its data fingerprint, params, and metrics recorded.

Usage:
    venv/bin/python src/register_model.py               # default params
    venv/bin/python src/register_model.py --depth 6     # a challenger

The newest registered version becomes champion (what the API/dashboard serve).
Roll back anytime with registry.set_champion("v1").
"""
import argparse
import sys

sys.path.insert(0, "src")

import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import average_precision_score, precision_score, recall_score
from xgboost import XGBClassifier

import registry

RANDOM_STATE = 42
FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--estimators", type=int, default=300)
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    # --- data (with fingerprint for the audit trail) ---
    raw = pd.read_csv("data/creditcard.csv")
    df = raw.drop_duplicates()
    X, y = df.drop(columns=["Class"]), df["Class"]
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
    )
    spw = (y_tr == 0).sum() / (y_tr == 1).sum()
    data_info = {
        "source": "data/creditcard.csv",
        "rows_raw": int(len(raw)),
        "rows_after_dedup": int(len(df)),
        "fraud_count": int(y.sum()),
        "test_size": 0.20,
        "random_state": RANDOM_STATE,
        "stratified": True,
    }

    # --- train ---
    params = {
        "n_estimators": args.estimators,
        "max_depth": args.depth,
        "learning_rate": 0.1,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "scale_pos_weight": round(float(spw), 2),
        "eval_metric": "aucpr",
        "random_state": RANDOM_STATE,
    }
    model = XGBClassifier(n_jobs=-1, **params)
    model.fit(X_tr[FEATURES], y_tr)

    # --- evaluate (metrics go into the version record) ---
    proba = model.predict_proba(X_te[FEATURES])[:, 1]
    pred = (proba >= 0.5).astype(int)
    metrics = {
        "recall": round(float(recall_score(y_te, pred)), 4),
        "precision": round(float(precision_score(y_te, pred)), 4),
        "pr_auc": round(float(average_precision_score(y_te, proba)), 4),
        "threshold": 0.5,
    }

    version = registry.register(
        model, metrics=metrics, params=params, data_info=data_info,
        features=FEATURES, notes=args.notes or f"depth={args.depth}",
    )
    print(f"Registered {version} as champion.")
    print(f"  metrics: {metrics}")
    print(f"  data:    {data_info['rows_after_dedup']:,} rows "
          f"({data_info['fraud_count']} fraud)")


if __name__ == "__main__":
    main()
