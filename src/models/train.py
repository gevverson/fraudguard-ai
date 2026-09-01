"""
train.py — Baseline fraud model for FraudGuard AI.

Implements the baseline from ARCHITECTURE.md: XGBoost + class weighting,
evaluated the HONEST way for a 0.173%-fraud problem — PR-AUC and recall at a
fixed false-positive rate, on a time-based split. Accuracy is deliberately NOT
used as a headline (see decision 1 below).

Run:  venv/bin/python src/models/train.py
Outputs: src/models/baseline_xgb.json (model), reports/metrics.json (numbers),
         reports/figures/pr_curve.png
"""
import json
import os

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)
from xgboost import XGBClassifier

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = "data/creditcard.csv"
MODEL_OUT = "src/models/baseline_xgb.json"
METRICS_OUT = "reports/metrics.json"
FIG_DIR = "reports/figures"
TARGET_FPR = 0.001  # business rule: tolerate ~0.1% false alarms on legit tx


def main():
    os.makedirs(FIG_DIR, exist_ok=True)
    os.makedirs("src/models", exist_ok=True)
    df = pd.read_csv(DATA)

    # --- Decision 3: data hygiene ------------------------------------------
    # Drop exact duplicate rows. If the same row lands in train AND test, the
    # model "memorizes" it and our scores lie. Dedupe BEFORE splitting.
    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    print(f"[hygiene] dropped {before - len(df)} duplicate rows -> {len(df):,} rows")

    # --- Decision 2: honest, time-based split ------------------------------
    # 'Time' = seconds since the first transaction. Train on the earlier 70%,
    # test on the later 30% — i.e. predict the *future* from the *past*.
    df = df.sort_values("Time").reset_index(drop=True)
    cut = int(len(df) * 0.70)
    train, test = df.iloc[:cut], df.iloc[cut:]

    features = [c for c in df.columns if c != "Class"]
    X_train, y_train = train[features], train["Class"]
    X_test, y_test = test[features], test["Class"]
    print(f"[split] train: {len(X_train):,} rows, {int(y_train.sum())} fraud | "
          f"test: {len(X_test):,} rows, {int(y_test.sum())} fraud")

    # Teaching aside: the "never fraud" baseline. This is what accuracy rewards.
    naive_acc = 1 - y_test.mean()
    print(f"[baseline] a model that predicts 'never fraud' scores "
          f"{naive_acc*100:.3f}% accuracy and catches 0 fraud. "
          f"That's why we don't use accuracy.")

    # --- Decision 4: imbalance via class weighting -------------------------
    pos = max(int((y_train == 1).sum()), 1)
    neg = int((y_train == 0).sum())
    pos_weight = neg / pos
    print(f"[imbalance] scale_pos_weight = {pos_weight:.0f} "
          f"(a missed fraud costs the loss ~{pos_weight:.0f}x a false alarm)")

    # --- Decision 5: XGBoost (no scaling needed for trees) -----------------
    model = XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        scale_pos_weight=pos_weight,
        eval_metric="aucpr",
        n_jobs=-1,
        random_state=42,
    )
    model.fit(X_train, y_train)

    # --- Decision 1: evaluate with metrics that survive imbalance ----------
    proba = model.predict_proba(X_test)[:, 1]
    pr_auc = float(average_precision_score(y_test, proba))  # area under PR curve
    roc_auc = float(roc_auc_score(y_test, proba))

    # Operating point: pick the threshold with the HIGHEST recall while keeping
    # the false-positive rate <= TARGET_FPR. This is the business choice — how
    # many legit customers are we willing to annoy to catch more fraud.
    fpr, tpr, thr_roc = roc_curve(y_test, proba)
    ok = fpr <= TARGET_FPR
    idx = np.where(ok)[0][np.argmax(tpr[ok])]
    chosen_thr = float(thr_roc[idx])

    pred = (proba >= chosen_thr).astype(int)
    tn, fp, fn, tp = (int(v) for v in confusion_matrix(y_test, pred).ravel())
    recall_op = tp / (tp + fn) if (tp + fn) else 0.0
    precision_op = tp / (tp + fp) if (tp + fp) else 0.0
    fpr_op = fp / (fp + tn) if (fp + tn) else 0.0

    metrics = {
        "test_transactions": int(len(y_test)),
        "test_fraud": int(y_test.sum()),
        "pr_auc": round(pr_auc, 4),
        "roc_auc": round(roc_auc, 4),
        "operating_point": {
            "target_fpr": TARGET_FPR,
            "threshold": round(chosen_thr, 4),
            "recall": round(recall_op, 4),
            "precision": round(precision_op, 4),
            "false_positive_rate": round(fpr_op, 5),
            "fraud_caught": tp,
            "fraud_missed": fn,
            "false_alarms": fp,
        },
    }

    # PR curve figure (proof for the case study)
    prec, rec, _ = precision_recall_curve(y_test, proba)
    plt.figure(figsize=(6, 4))
    plt.plot(rec, prec, color="#e76f51")
    plt.xlabel("Recall (fraud caught)"); plt.ylabel("Precision (flags that were right)")
    plt.title(f"Precision-Recall curve — PR-AUC = {pr_auc:.3f}")
    plt.grid(alpha=.3); plt.tight_layout()
    plt.savefig(f"{FIG_DIR}/pr_curve.png", dpi=110); plt.close()

    model.save_model(MODEL_OUT)
    with open(METRICS_OUT, "w") as fh:
        json.dump(metrics, fh, indent=2)

    print("\n=== RESULTS (on the future/test slice) ===")
    print(f"PR-AUC  : {pr_auc:.3f}   (0.00173 would be random guessing)")
    print(f"ROC-AUC : {roc_auc:.3f}")
    print(f"At <= {TARGET_FPR*100:.2f}% false-positive rate:")
    print(f"  recall    : {recall_op*100:.1f}%  ({tp} of {tp+fn} frauds caught, {fn} missed)")
    print(f"  precision : {precision_op*100:.1f}%  ({fp} false alarms on {tn+fp:,} legit)")
    print(f"\nSaved model -> {MODEL_OUT}")
    print(f"Saved metrics -> {METRICS_OUT}")
    print(f"Saved PR curve -> {FIG_DIR}/pr_curve.png")


if __name__ == "__main__":
    main()
