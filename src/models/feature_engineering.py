"""
feature_engineering.py — can we make the model smarter with better features?

Feature engineering = creating NEW columns that expose signal the raw data hides.
In real fraud systems this is the single biggest lever. But there's an honest
catch with THIS public dataset, and learning it matters:

  - V1..V28 are already PCA-transformed — the bank pre-squeezed the signal out of
    the raw fields, so we can't engineer on them.
  - There is NO card/account/device ID — so we CANNOT build the most powerful
    real-world features: per-card VELOCITY ("how many tx from this card in the
    last minute"). That's the #1 fraud feature and this data can't express it.

So we engineer what the raw columns (Time, Amount) DO allow, measure honestly, and
note what we'd add on a bank's real data.

We compare fairly: SAME train/test rows, one model on base features, one on
base + engineered features.

Run:  venv/bin/python src/models/feature_engineering.py
"""
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import average_precision_score, recall_score, precision_score
from xgboost import XGBClassifier

RANDOM_STATE = 42
BASE = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]

df = pd.read_csv("data/creditcard.csv").drop_duplicates().sort_values("Time").reset_index(drop=True)

# --- Engineered features (from the only raw columns we have: Time, Amount) ---
df["Amount_log"] = np.log1p(df["Amount"])                 # tame the skew
df["Time_delta"] = df["Time"].diff().fillna(0)            # secs since prev tx (burst signal)
hour = (df["Time"] // 3600) % 24
df["hour_sin"] = np.sin(2 * np.pi * hour / 24)            # cyclical time-of-day
df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
ENGINEERED = ["Amount_log", "Time_delta", "hour_sin", "hour_cos"]

y = df["Class"]
X = df[BASE + ENGINEERED]

# One split, reused for both models -> a fair A/B test
X_tr, X_te, y_tr, y_te = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
)
spw = (y_tr == 0).sum() / (y_tr == 1).sum()


def train_and_score(cols, label):
    m = XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.1,
        subsample=0.9, colsample_bytree=0.9, scale_pos_weight=spw,
        eval_metric="aucpr", n_jobs=-1, random_state=RANDOM_STATE,
    )
    m.fit(X_tr[cols], y_tr)
    p = m.predict_proba(X_te[cols])[:, 1]
    pred = (p >= 0.5).astype(int)
    print(f"{label:<22} recall={recall_score(y_te, pred)*100:5.1f}%  "
          f"precision={precision_score(y_te, pred)*100:5.1f}%  "
          f"PR-AUC={average_precision_score(y_te, p):.4f}")
    return average_precision_score(y_te, p), m


print("\n=== FAIR A/B TEST (same rows, threshold 0.5) ===")
base_ap, _ = train_and_score(BASE, "Base features")
eng_ap, eng_model = train_and_score(BASE + ENGINEERED, "Base + engineered")

delta = (eng_ap - base_ap)
print(f"\nPR-AUC change from engineering: {delta:+.4f} "
      f"({'better' if delta > 0 else 'no real gain'})")

# Did the model actually USE the new features? Rank them by importance.
imp = pd.Series(eng_model.feature_importances_, index=BASE + ENGINEERED).sort_values(ascending=False)
print("\nWhere our engineered features landed (importance rank out of "
      f"{len(imp)}):")
for f in ENGINEERED:
    rank = list(imp.index).index(f) + 1
    print(f"  {f:<12} rank #{rank:<3} importance={imp[f]:.4f}")

print("\nTakeaway:")
print("  On THIS anonymized data, gains are small — the signal is already inside")
print("  V1..V28. The real jackpot (per-card velocity) needs an account/device ID")
print("  this public set doesn't have. On a bank's RAW data, that's where the big")
print("  accuracy jump comes from. That gap is a selling point, not a weakness.")
