"""
explain.py — WHY did the model flag this transaction? (Explainable AI / SHAP)

A risk analyst will not act on a naked score ("87% fraud"). They need the reason.
SHAP answers, per transaction: which features pushed this toward FRAUD, and how
much. It turns the black box into evidence an analyst (and a regulator) trusts.

Two views:
  - GLOBAL: which features drive fraud decisions overall?  (summary plots)
  - LOCAL : for ONE flagged transaction, the exact reasons.  (printed + waterfall)

Run:  venv/bin/python src/models/explain.py
"""
import os
import joblib
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import shap
from sklearn.model_selection import train_test_split

RANDOM_STATE = 42
FIG_DIR = "reports/figures"
os.makedirs(FIG_DIR, exist_ok=True)

# Rebuild the same test set + load the trained model
df = pd.read_csv("data/creditcard.csv").drop_duplicates()
X, y = df.drop(columns=["Class"]), df["Class"]
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
)
model = joblib.load("models/baseline_xgb.joblib")
proba = model.predict_proba(X_test)[:, 1]

# TreeExplainer is exact and fast for tree models like XGBoost
explainer = shap.TreeExplainer(model)

# ---------------------------------------------------------------------------
# GLOBAL — what drives fraud decisions overall
# ---------------------------------------------------------------------------
sample = X_test.sample(3000, random_state=RANDOM_STATE)
sv_sample = explainer.shap_values(sample)

mean_abs = np.abs(sv_sample).mean(axis=0)
ranking = pd.Series(mean_abs, index=X_test.columns).sort_values(ascending=False)
print("\n=== GLOBAL: features that drive the model most ===")
for feat, val in ranking.head(8).items():
    print(f"  {feat:>7}: {val:.3f} average impact")

plt.figure()
shap.summary_plot(sv_sample, sample, plot_type="bar", show=False)
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/shap_global_bar.png", dpi=110, bbox_inches="tight"); plt.close()

plt.figure()
shap.summary_plot(sv_sample, sample, show=False)   # beeswarm: direction + value
plt.tight_layout(); plt.savefig(f"{FIG_DIR}/shap_beeswarm.png", dpi=110, bbox_inches="tight"); plt.close()

# ---------------------------------------------------------------------------
# LOCAL — explain ONE real fraud the model caught
# ---------------------------------------------------------------------------
caught_fraud_idx = X_test.index[(y_test == 1).values & (proba >= 0.5)][0]
row = X_test.loc[[caught_fraud_idx]]
sv_one = explainer.shap_values(row)[0]                 # shap value per feature
base = explainer.expected_value                        # model's starting point
contrib = pd.Series(sv_one, index=X_test.columns)
top = contrib.reindex(contrib.abs().sort_values(ascending=False).index).head(6)

print(f"\n=== LOCAL: why transaction #{caught_fraud_idx} was flagged ===")
print(f"Model's fraud probability for it: {proba[list(X_test.index).index(caught_fraud_idx)]*100:.1f}%  "
      f"(true label: {'FRAUD' if y_test.loc[caught_fraud_idx]==1 else 'legit'})")
print("Top reasons (SHAP contribution, + = pushed toward FRAUD):")
for feat, val in top.items():
    direction = "→ FRAUD " if val > 0 else "→ legit "
    print(f"  {feat:>7} = {row[feat].values[0]:>10.3f}   {direction} (weight {val:+.2f})")

# waterfall for that one transaction
try:
    expl = shap.Explanation(values=sv_one, base_values=base,
                            data=row.values[0], feature_names=list(X_test.columns))
    plt.figure()
    shap.plots.waterfall(expl, max_display=8, show=False)
    plt.tight_layout(); plt.savefig(f"{FIG_DIR}/shap_one_transaction.png", dpi=110, bbox_inches="tight"); plt.close()
    print("\nSaved waterfall -> reports/figures/shap_one_transaction.png")
except Exception as e:
    print(f"(waterfall plot skipped: {e})")

print("Saved global plots -> shap_global_bar.png, shap_beeswarm.png")
print("Done.")
