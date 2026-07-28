"""
monitor_demo.py — prove the drift monitor (B3) works both ways:
  1. Stable traffic (same distribution) -> PSI low -> "stable" (no false alarm).
  2. Shifted traffic (injected drift)   -> PSI high -> "alert" (warning fires).

Run:  venv/bin/python src/monitor_demo.py
"""
import sys
sys.path.insert(0, "src")

import pandas as pd
from sklearn.model_selection import train_test_split
import monitor

MONITORED = ["Amount", "V14", "V10", "V12", "V4", "V17"]

df = pd.read_csv("data/creditcard.csv").drop_duplicates()
X, y = df.drop(columns=["Class"]), df["Class"]
# Reference = the training slice (what the model learned from)
X_train, X_test, _, _ = train_test_split(X, y, test_size=0.20, stratify=y, random_state=42)
reference = X_train

# --- 1. STABLE: a fresh sample from the same world ---
stable = X_test.sample(4000, random_state=1)
report, overall = monitor.drift_report(reference, stable, MONITORED)
print("=== 1. STABLE traffic (same distribution) ===")
print(report.to_string(index=False))
print(f"OVERALL: {overall.upper()}  <- expected 'stable', no false alarm\n")

# --- 2. DRIFTED: same sample, but the world shifted ---
drifted = X_test.sample(4000, random_state=1).copy()
drifted["Amount"] = drifted["Amount"] * 4        # spend patterns jumped
drifted["V14"] = drifted["V14"] + 2.5            # a behavioural signal shifted
drifted["V4"] = drifted["V4"] - 3.0
report, overall = monitor.drift_report(reference, drifted, MONITORED)
print("=== 2. DRIFTED traffic (injected shift) ===")
print(report.to_string(index=False))
print(f"OVERALL: {overall.upper()}  <- warning light fires; time to investigate/retrain")
