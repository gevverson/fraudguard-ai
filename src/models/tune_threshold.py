"""
tune_threshold.py — The DIAL. This is the heart of your product.

A model outputs a fraud PROBABILITY (0..1) for every transaction. YOU choose the
cut-off: "flag anything above X." That cut-off is the dial.

  - Turn it DOWN (e.g. 0.10): catch more fraud, but annoy more good customers.
  - Turn it UP   (e.g. 0.90): fewer false alarms, but let more fraud through.

There is no perfect setting — only the one that fits a client's risk appetite.
This script shows the whole trade-off so you can SET the dial on purpose.

Run:  venv/bin/python src/models/tune_threshold.py
"""
import os
import joblib
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_recall_curve

RANDOM_STATE = 42
FIG_DIR = "reports/figures"
os.makedirs(FIG_DIR, exist_ok=True)

# Rebuild the EXACT same test set the model was evaluated on (same clean+split)
df = pd.read_csv("data/creditcard.csv").drop_duplicates()
X, y = df.drop(columns=["Class"]), df["Class"]
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
)

model = joblib.load("models/baseline_xgb.joblib")
proba = model.predict_proba(X_test)[:, 1]          # fraud probability per tx
amt = X_test["Amount"].values
is_fraud = (y_test == 1).values
total_fraud_amt = amt[is_fraud].sum()

# ---------------------------------------------------------------------------
# The dial at several settings — one row per cut-off
# ---------------------------------------------------------------------------
print("\n=== THE DIAL: what each cut-off buys and costs (test set, 95 frauds) ===")
header = f"{'cut-off':>8} | {'fraud caught':>12} | {'recall':>7} | {'false alarms':>12} | {'precision':>9} | {'fraud $ saved':>13}"
print(header); print("-" * len(header))
for t in [0.95, 0.90, 0.70, 0.50, 0.30, 0.10, 0.05, 0.01]:
    pred = proba >= t
    tp = int((pred & is_fraud).sum())
    fp = int((pred & ~is_fraud).sum())
    fn = int((~pred & is_fraud).sum())
    recall = tp / (tp + fn) if (tp + fn) else 0
    precision = tp / (tp + fp) if (tp + fp) else 0
    saved = amt[pred & is_fraud].sum()
    print(f"{t:>8.2f} | {tp:>4}/{tp+fn:<7} | {recall*100:>6.1f}% | "
          f"{fp:>12} | {precision*100:>8.1f}% | ${saved:>11,.0f}")

print("\nRead it top-to-bottom: as the cut-off drops, 'fraud caught' rises but")
print("'false alarms' rise too. That is the trade-off you are selling.")

# ---------------------------------------------------------------------------
# Figure 1: the Precision-Recall curve (every possible dial setting at once)
# ---------------------------------------------------------------------------
prec, rec, _ = precision_recall_curve(y_test, proba)
plt.figure(figsize=(6, 5))
plt.plot(rec, prec, color="#e76f51", lw=2)
plt.xlabel("Recall (fraud caught)"); plt.ylabel("Precision (flags that are real)")
plt.title("Precision–Recall curve\n(each point = one dial setting)")
plt.grid(alpha=.3); plt.tight_layout()
plt.savefig(f"{FIG_DIR}/pr_curve.png", dpi=110); plt.close()

# ---------------------------------------------------------------------------
# Figure 2: the dial itself — recall vs false alarms as the cut-off moves
# ---------------------------------------------------------------------------
ts = np.linspace(0.01, 0.99, 99)
recalls = [ (proba >= t)[is_fraud].mean() * 100 for t in ts ]
false_alarms = [ int(((proba >= t) & ~is_fraud).sum()) for t in ts ]
fig, ax1 = plt.subplots(figsize=(7, 5))
ax1.set_xlabel("Cut-off (the dial)")
ax1.set_ylabel("Fraud caught  (recall %)", color="#2a9d8f")
ax1.plot(ts, recalls, color="#2a9d8f", lw=2)
ax1.tick_params(axis="y", labelcolor="#2a9d8f")
ax2 = ax1.twinx()
ax2.set_ylabel("Good customers wrongly blocked (count)", color="#e76f51")
ax2.plot(ts, false_alarms, color="#e76f51", lw=2)
ax2.tick_params(axis="y", labelcolor="#e76f51")
plt.title("The dial: turn it left = catch more fraud AND block more good customers")
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/the_dial.png", dpi=110); plt.close()

print(f"\nSaved 2 figures to {FIG_DIR}/  (pr_curve.png, the_dial.png)")
print("Done.")
