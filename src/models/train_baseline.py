"""
train_baseline.py — FraudGuard AI's first detection model (the "brain", v1).

What it does, in plain terms:
  1. Load the transactions and REMOVE duplicates (so the model can't cheat).
  2. Split into train/test the RIGHT way (stratified) so the test set mirrors
     the real 0.173% fraud rate.
  3. Train an XGBoost model, telling it fraud matters ~578x more (class weight)
     so it can't win by ignoring fraud.
  4. Judge it with the metrics banks care about (recall, precision, PR-AUC) —
     never plain accuracy.
  5. Translate the results into DOLLARS, which is how you'll pitch it.

Run:  venv/bin/python src/models/train_baseline.py
"""
import os
import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    classification_report, confusion_matrix,
    average_precision_score, roc_auc_score,
)
from xgboost import XGBClassifier

RANDOM_STATE = 42
os.makedirs("models", exist_ok=True)

# ---------------------------------------------------------------------------
# 1. LOAD + CLEAN
# ---------------------------------------------------------------------------
df = pd.read_csv("data/creditcard.csv")
before = len(df)
df = df.drop_duplicates()                       # <-- the 1,081 duplicates, gone
print(f"Dropped {before - len(df):,} duplicate rows. Remaining: {len(df):,}")

X = df.drop(columns=["Class"])                  # features: Time, V1..V28, Amount
y = df["Class"]                                 # label: 1 = fraud, 0 = legit

# ---------------------------------------------------------------------------
# 2. STRATIFIED SPLIT
#    'stratify=y' forces the test set to keep the SAME 0.173% fraud rate as
#    reality. Without it, a random split might land very few frauds in the test
#    set and our score would be meaningless (luck, not skill).
# ---------------------------------------------------------------------------
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
)
print(f"Train: {len(X_train):,} rows ({int(y_train.sum())} fraud) | "
      f"Test: {len(X_test):,} rows ({int(y_test.sum())} fraud)")

# ---------------------------------------------------------------------------
# 3. TRAIN — with class weighting to beat the imbalance
#    scale_pos_weight = (#legit / #fraud) in the TRAINING set. This tells
#    XGBoost: "a fraud you miss hurts ~this many times more than a false alarm."
# ---------------------------------------------------------------------------
spw = (y_train == 0).sum() / (y_train == 1).sum()
print(f"\nClass weight (scale_pos_weight): {spw:.1f}  "
      f"(1 fraud is treated as worth ~{spw:.0f} legit rows)")

model = XGBClassifier(
    n_estimators=300,
    max_depth=4,
    learning_rate=0.1,
    subsample=0.9,
    colsample_bytree=0.9,
    scale_pos_weight=spw,       # <-- the imbalance fix
    eval_metric="aucpr",        # optimize precision-recall area, not accuracy
    n_jobs=-1,
    random_state=RANDOM_STATE,
)
model.fit(X_train, y_train)

# ---------------------------------------------------------------------------
# 4. EVALUATE — the honest metrics
# ---------------------------------------------------------------------------
proba = model.predict_proba(X_test)[:, 1]       # fraud probability 0..1
pred = (proba >= 0.50).astype(int)              # decision at the 0.50 dial

print("\n=== HOW GOOD IS IT? (test set) ===")
print(classification_report(y_test, pred, target_names=["legit", "fraud"], digits=3))

tn, fp, fn, tp = confusion_matrix(y_test, pred).ravel()
print("Confusion matrix:")
print(f"  True Positives  (fraud caught)        : {tp}")
print(f"  False Negatives (fraud MISSED)        : {fn}")
print(f"  False Positives (good customer blocked): {fp}")
print(f"  True Negatives  (legit approved)      : {tn:,}")

print(f"\nRecall  (fraud caught)      : {tp/(tp+fn)*100:.1f}%")
print(f"Precision (flags that were real): {tp/(tp+fp)*100:.1f}%")
print(f"PR-AUC  (overall skill, 0-1): {average_precision_score(y_test, proba):.3f}")
print(f"ROC-AUC                      : {roc_auc_score(y_test, proba):.3f}")

# ---------------------------------------------------------------------------
# 5. THE PART A BANK CARES ABOUT — money
# ---------------------------------------------------------------------------
amt_test = X_test["Amount"].values
is_fraud = (y_test == 1).values
caught_mask  = (pred == 1) & is_fraud
missed_mask  = (pred == 0) & is_fraud
blocked_good = (pred == 1) & (~is_fraud)

print("\n=== IN DOLLARS (on the test slice) ===")
print(f"Fraud $ CAUGHT (prevented)     : ${amt_test[caught_mask].sum():,.2f}")
print(f"Fraud $ MISSED (still lost)    : ${amt_test[missed_mask].sum():,.2f}")
print(f"Good customers wrongly blocked : {blocked_good.sum()} "
      f"(${amt_test[blocked_good].sum():,.2f} of legit spend)")
prevented = amt_test[caught_mask].sum()
total_fraud_amt = amt_test[is_fraud].sum()
print(f"So we stopped {prevented/total_fraud_amt*100:.1f}% of fraud VALUE in this slice.")

# ---------------------------------------------------------------------------
# 6. SAVE the trained brain so the API/dashboard can reuse it later
# ---------------------------------------------------------------------------
joblib.dump(model, "models/baseline_xgb.joblib")
print("\nSaved model -> models/baseline_xgb.joblib")
print("Done.")
