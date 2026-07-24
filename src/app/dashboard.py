"""
dashboard.py — FraudGuard AI demo dashboard (Streamlit).

This is the FACE you pitch. It shows, on one screen:
  1. THE PROBLEM  — how rare and costly fraud is.
  2. THE DIAL     — a slider a prospect can move to feel the trade-off.
  3. THE FIX      — run detection on live-looking transactions, catch fraud,
                    show WHY (SHAP reasons), and tally $ prevented (ROI).

Run:  venv/bin/streamlit run src/app/dashboard.py
"""
import joblib
import numpy as np
import pandas as pd
import shap
import streamlit as st
from sklearn.model_selection import train_test_split

RANDOM_STATE = 42
FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]


# --- pure logic (no Streamlit), so it is testable on its own -----------------
def load_assets():
    df = pd.read_csv("data/creditcard.csv").drop_duplicates()
    X, y = df.drop(columns=["Class"]), df["Class"]
    _, X_test, _, y_test = train_test_split(
        X, y, test_size=0.20, stratify=y, random_state=RANDOM_STATE
    )
    model = joblib.load("models/baseline_xgb.joblib")
    explainer = shap.TreeExplainer(model)
    return model, explainer, X_test, y_test


def run_detection(assets, n, review_at, decline_at, seed):
    """Score a sample and return (summary dict, flagged-transactions DataFrame)."""
    model, explainer, X_test, y_test = assets
    Xs = X_test.sample(min(n, len(X_test)), random_state=seed)
    ys = y_test.loc[Xs.index].values
    proba = model.predict_proba(Xs[FEATURES])[:, 1]

    decision = np.where(proba >= decline_at, "decline",
                np.where(proba >= review_at, "review", "approve"))
    flagged = decision != "approve"          # review or decline = not auto-approved
    is_fraud = ys == 1
    amt = Xs["Amount"].values

    summary = {
        "total": len(Xs),
        "actual_fraud": int(is_fraud.sum()),
        "caught": int((flagged & is_fraud).sum()),
        "missed": int((~flagged & is_fraud).sum()),
        "false_alarms": int((flagged & ~is_fraud).sum()),
        "prevented_amt": float(amt[flagged & is_fraud].sum()),
        "missed_amt": float(amt[~flagged & is_fraud].sum()),
    }

    # Build the flagged table with a SHAP top-reason for each
    idx = np.where(flagged)[0]
    rows = []
    if len(idx):
        sv = explainer.shap_values(Xs[FEATURES].iloc[idx])
        for j, i in enumerate(idx):
            top_f = FEATURES[int(np.argmax(np.abs(sv[j])))]
            rows.append({
                "risk_score": round(float(proba[i]) * 100, 1),
                "decision": decision[i],
                "amount": round(float(amt[i]), 2),
                "actual": "FRAUD" if is_fraud[i] else "legit",
                "top_reason": f"{top_f} (→{'fraud' if sv[j][FEATURES.index(top_f)] > 0 else 'legit'})",
            })
    table = pd.DataFrame(rows).sort_values("risk_score", ascending=False) if rows else pd.DataFrame()
    return summary, table


# --- Streamlit UI ------------------------------------------------------------
def main():
    st.set_page_config(page_title="FraudGuard AI", page_icon="🛡️", layout="wide")
    assets = st.cache_resource(load_assets)()

    st.title("🛡️ FraudGuard AI — Live Fraud Detection")
    st.caption("Defensive card-fraud scoring · metadata only, no card numbers · explainable decisions")

    # 1. THE PROBLEM
    st.subheader("1 · The problem")
    c1, c2, c3 = st.columns(3)
    c1.metric("Fraud rate", "0.173%", "1 in 578 transactions")
    c2.metric("The challenge", "needle in haystack", "‘accuracy’ is a trap")
    c3.metric("Our model's skill", "0.82 PR-AUC", "useless model ≈ 0.002")

    # 2. THE DIAL
    st.sidebar.header("🎛️ The dial")
    st.sidebar.caption("Move these to fit a client's risk appetite — no retraining.")
    review_at = st.sidebar.slider("Send to REVIEW above", 0.05, 0.95, 0.30, 0.05)
    decline_at = st.sidebar.slider("Auto-DECLINE above", review_at, 0.99, 0.80, 0.05)
    n = st.sidebar.select_slider("Transactions to scan", [500, 1000, 2000, 5000, 10000], 2000)
    seed = st.sidebar.number_input("Sample seed", 0, 9999, 42)

    # 3. THE FIX
    st.subheader("2 · The fix — run detection")
    summary, table = run_detection(assets, n, review_at, decline_at, seed)

    m1, m2, m3, m4 = st.columns(4)
    recall = summary["caught"] / summary["actual_fraud"] * 100 if summary["actual_fraud"] else 0
    m1.metric("Fraud caught", f"{summary['caught']}/{summary['actual_fraud']}", f"{recall:.0f}% recall")
    m2.metric("Fraud missed", summary["missed"])
    m3.metric("Good customers flagged", summary["false_alarms"])
    m4.metric("💰 Fraud $ prevented", f"${summary['prevented_amt']:,.0f}",
              f"-${summary['missed_amt']:,.0f} still lost", delta_color="normal")

    st.subheader("3 · Flagged transactions — with the reason")
    if len(table):
        st.dataframe(
            table.style.apply(
                lambda r: ["background-color: #4a1010" if r["actual"] == "FRAUD"
                           else "background-color: #3a2a10"] * len(r), axis=1),
            use_container_width=True, height=380,
        )
    else:
        st.info("No transactions flagged at these dial settings — try lowering the review threshold.")

    st.caption("Every flag shows its top SHAP reason. On a bank's real data these become "
               "human-readable ('new device', 'country mismatch').")


if __name__ == "__main__":
    main()
