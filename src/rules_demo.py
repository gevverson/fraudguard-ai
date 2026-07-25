"""
rules_demo.py — prove the rules layer (B1) works and combines with the ML.

Shows, for real transactions:
  ML decision  +  Rules decision  ->  Final (the stricter one).

The headline case: a transaction the ML would APPROVE, but a rule sends to
REVIEW — that's the floor catching what the drifting model misses.

Run:  venv/bin/python src/rules_demo.py
"""
import sys
sys.path.insert(0, "src")

import joblib
import pandas as pd
from sklearn.model_selection import train_test_split
import rules

FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]
REVIEW_AT, DECLINE_AT = 0.30, 0.80


def ml_decision(p):
    return "decline" if p >= DECLINE_AT else "review" if p >= REVIEW_AT else "approve"


df = pd.read_csv("data/creditcard.csv").drop_duplicates()
X, y = df.drop(columns=["Class"]), df["Class"]
_, X_test, _, _ = train_test_split(X, y, test_size=0.20, stratify=y, random_state=42)
model = joblib.load("models/baseline_xgb.joblib")

# Pick transactions that exercise each rule
examples = {
    "$0 auth (card-test probe)": X_test[X_test.Amount == 0].iloc[0],
    "single micro charge < $1":  X_test[(X_test.Amount > 0) & (X_test.Amount < 1)].iloc[0],
    "very high amount":         X_test[X_test.Amount >= 10_000].iloc[0],
    "normal purchase":          X_test[(X_test.Amount > 40) & (X_test.Amount < 60)].iloc[0],
}

print("\n=== RULES + ML COMBINED (final = the stricter one) ===\n")
print(f"{'transaction':<28} {'amount':>10} | {'ML':>8} | {'rules':>8} | {'FINAL':>8} | reason")
print("-" * 100)
for label, row in examples.items():
    tx = row.to_dict()
    p = float(model.predict_proba(pd.DataFrame([row[FEATURES]]))[0, 1])
    mld = ml_decision(p)
    final, hits = rules.combine(mld, tx)
    reason = hits[0].reason if hits else "—"
    flag = "  <-- rule caught it!" if rules.ORDER[final] > rules.ORDER[mld] else ""
    print(f"{label:<28} {tx['Amount']:>10,.2f} | {mld:>8} | "
          f"{(hits[0].action if hits else 'approve'):>8} | {final:>8} | {reason}{flag}")

print("\nNote: a SINGLE micro-charge is NOT flagged — that would flood the queue with")
print("legit tiny purchases. Card-testing is a BURST, which needs velocity context:")

# Velocity rules fire once real per-card context is supplied
print("\n=== VELOCITY RULES (inert on this data; fire with real per-card history) ===")
micro = examples["single micro charge < $1"].to_dict()
final, hits = rules.combine("approve", micro, context={"tx_count_last_60s": 4})
print(f"That same ${micro['Amount']:.2f} charge, but 4 tiny charges on the card in 60s:")
print(f"  ML said 'approve' -> FINAL '{final}'  because: {hits[0].reason}")

tx = examples["normal purchase"].to_dict()
final, hits = rules.combine("approve", tx, context={"tx_count_last_60s": 23})
print(f"A normal purchase, but 23 tx on the card in 60s:")
print(f"  ML said 'approve' -> FINAL '{final}'  because: {hits[0].reason}")
print("\nThat is the floor: even if the model is fooled, the rule holds the line —")
print("and it's precise, so it doesn't cry wolf on normal traffic.")
