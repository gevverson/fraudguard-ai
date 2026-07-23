"""
01_explore.py — Guided exploratory data analysis (EDA) for FraudGuard AI.

Goal: UNDERSTAND the data before modelling. We answer 5 questions:
  1. How imbalanced is it, exactly?
  2. Are fraudulent amounts different from legit ones?
  3. Does fraud cluster in time?
  4. Which features separate fraud from legit the most?
  5. Is the data clean (missing values, scaling)?

Run:  venv/bin/python notebooks/01_explore.py
Figures are saved to reports/figures/ so you can open and study them.
"""
import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")           # no screen needed; we save PNGs
import matplotlib.pyplot as plt

pd.set_option("display.width", 120)
FIG_DIR = "reports/figures"
os.makedirs(FIG_DIR, exist_ok=True)

df = pd.read_csv("data/creditcard.csv")

# ---------------------------------------------------------------------------
print("\n=== Q1. HOW IMBALANCED? ===")
n = len(df)
counts = df["Class"].value_counts()
fraud, legit = int(counts.get(1, 0)), int(counts.get(0, 0))
print(f"Total transactions : {n:,}")
print(f"Legit (Class=0)    : {legit:,}  ({legit/n*100:.3f}%)")
print(f"Fraud (Class=1)    : {fraud:,}  ({fraud/n*100:.3f}%)")
print(f"Imbalance ratio    : 1 fraud for every {legit/fraud:,.0f} legit transactions")

# ---------------------------------------------------------------------------
print("\n=== Q2. ARE FRAUD AMOUNTS DIFFERENT? ===")
amt = df.groupby("Class")["Amount"]
summary = amt.agg(["count", "mean", "median", "min", "max"]).round(2)
summary.index = ["legit", "fraud"]
print(summary)
print(f"\n% of fraud that are 'small tests' (Amount < $1): "
      f"{(df[df.Class==1].Amount < 1).mean()*100:.1f}%")

# ---------------------------------------------------------------------------
print("\n=== Q3. DOES FRAUD CLUSTER IN TIME? ===")
# 'Time' = seconds since the first transaction. Data spans ~2 days.
df["hour"] = (df["Time"] // 3600) % 24        # hour of day (0-23), approx
rate_by_hour = df.groupby("hour")["Class"].mean() * 100
print(f"Data spans {df.Time.max()/3600:.1f} hours (~{df.Time.max()/86400:.1f} days)")
print(f"Overall fraud rate      : {fraud/n*100:.3f}%")
print(f"Worst hour fraud rate   : {rate_by_hour.max():.3f}% (hour {rate_by_hour.idxmax()})")
print(f"Calmest hour fraud rate : {rate_by_hour.min():.3f}% (hour {rate_by_hour.idxmin()})")

# ---------------------------------------------------------------------------
print("\n=== Q4. WHICH FEATURES SEPARATE FRAUD BEST? ===")
# Correlation of each feature with the fraud label. Bigger |value| = more signal.
corr = df.drop(columns=["hour"]).corr()["Class"].drop("Class")
top = corr.reindex(corr.abs().sort_values(ascending=False).index).head(8)
print("Top 8 features by |correlation with fraud|:")
for feat, val in top.items():
    arrow = "higher in fraud" if val > 0 else "lower in fraud "
    print(f"  {feat:>6}: {val:+.3f}  ({arrow})")

# ---------------------------------------------------------------------------
print("\n=== Q5. IS THE DATA CLEAN? ===")
print(f"Missing values total : {int(df.isna().sum().sum())}")
print(f"Duplicate rows       : {int(df.duplicated().sum())}")
print(f"Amount range         : ${df.Amount.min():.2f} to ${df.Amount.max():,.2f}")
print("V1..V28 are already PCA-scaled (mean~0). Time & Amount are NOT scaled yet.")

# ---------------------------------------------------------------------------
# Figures for study
plt.figure(figsize=(5, 4))
counts.rename({0: "legit", 1: "fraud"}).plot(kind="bar", color=["#2a9d8f", "#e76f51"])
plt.yscale("log"); plt.title("Class balance (log scale)"); plt.ylabel("count (log)")
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/class_balance.png", dpi=110); plt.close()

plt.figure(figsize=(6, 4))
plt.hist(df[df.Class==0].Amount.clip(upper=500), bins=50, alpha=.6, label="legit", color="#2a9d8f", density=True)
plt.hist(df[df.Class==1].Amount.clip(upper=500), bins=50, alpha=.6, label="fraud", color="#e76f51", density=True)
plt.legend(); plt.title("Amount distribution (capped at $500)"); plt.xlabel("Amount")
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/amount_dist.png", dpi=110); plt.close()

plt.figure(figsize=(6, 4))
top.sort_values().plot(kind="barh", color=(top.sort_values() > 0).map({True: "#e76f51", False: "#264653"}))
plt.title("Top features correlated with fraud"); plt.xlabel("correlation with Class")
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/top_features.png", dpi=110); plt.close()

print(f"\nSaved 3 figures to {FIG_DIR}/  (class_balance, amount_dist, top_features)")
print("Done.")
