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
        "n_approve": int((decision == "approve").sum()),
        "n_review": int((decision == "review").sum()),
        "n_decline": int((decision == "decline").sum()),
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


def score_one(assets, row):
    """Score ONE transaction: return (probability, ranked SHAP reasons)."""
    model, explainer, _, _ = assets
    proba = float(model.predict_proba(row[FEATURES])[0, 1])
    sv = explainer.shap_values(row[FEATURES])[0]
    ranked = sorted(zip(FEATURES, sv), key=lambda kv: abs(kv[1]), reverse=True)[:5]
    reasons = [(f, float(v)) for f, v in ranked]
    return proba, reasons


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

    # Decision breakdown — show the ACTION QUEUE (review + decline), the bit a
    # bank acts on. Approve dominates the count, so it goes in the caption only.
    st.subheader("3 · The action queue")
    queue = pd.DataFrame(
        {"count": [summary["n_review"], summary["n_decline"]]},
        index=["🔍 review (OTP / check)", "⛔ decline"],
    )
    bcol, tcol = st.columns([1, 2])
    with bcol:
        st.bar_chart(queue, color="#e76f51", height=300, horizontal=True)
        st.caption(f"✅ {summary['n_approve']:,} auto-approved (no friction) · "
                   f"{summary['n_review'] + summary['n_decline']} need action")

    # Flagged transactions — cleanly formatted
    with tcol:
        st.markdown("**Flagged transactions — with the reason**")
        if len(table):
            disp = table.copy()
            disp["Risk"] = disp["risk_score"].map(lambda v: f"{v:.1f}%")
            disp["Amount"] = disp["amount"].map(lambda v: f"${v:,.2f}")
            disp = disp.rename(columns={"decision": "Decision", "actual": "Actual",
                                        "top_reason": "Top reason"})
            disp = disp[["Risk", "Decision", "Amount", "Actual", "Top reason"]]
            st.dataframe(
                disp.style.apply(
                    lambda r: ["background-color: #4a1010" if r["Actual"] == "FRAUD"
                               else "background-color: #3a2a10"] * len(r), axis=1),
                use_container_width=True, height=300, hide_index=True,
            )
        else:
            st.info("No transactions flagged at these dial settings — lower the review threshold to catch more.")

    st.caption("Every flag shows its top SHAP reason. On a bank's real data these become "
               "human-readable ('new device', 'country mismatch').")

    # 4. SCORE A SINGLE TRANSACTION — hands-on, uses the current dial
    st.divider()
    st.subheader("4 · Score one transaction")
    st.caption("Pick a transaction and see the exact decision it gets at your current dial settings.")
    _, _, X_test, y_test = assets
    b1, b2, _ = st.columns([1, 1, 3])
    if "pick_seed" not in st.session_state:
        st.session_state.pick_seed = 1
    if b1.button("🎲 Random transaction"):
        st.session_state.pick_seed += 1
        st.session_state.pick_kind = "random"
    if b2.button("🚨 A known fraud"):
        st.session_state.pick_seed += 1
        st.session_state.pick_kind = "fraud"

    if "pick_kind" in st.session_state:
        pool = X_test if st.session_state.pick_kind == "random" else X_test[y_test == 1]
        row = pool.sample(1, random_state=st.session_state.pick_seed)
        truth = "FRAUD" if y_test.loc[row.index[0]] == 1 else "legit"
        proba, reasons = score_one(assets, row)
        decision = ("decline" if proba >= decline_at
                    else "review" if proba >= review_at else "approve")

        left, right = st.columns([1, 2])
        with left:
            st.metric("Risk score", f"{proba*100:.1f}%", f"actually {truth}")
            if decision == "decline":
                st.error(f"⛔ DECLINE  (amount ${row['Amount'].values[0]:,.2f})")
            elif decision == "review":
                st.warning(f"🔍 REVIEW / step-up auth  (amount ${row['Amount'].values[0]:,.2f})")
            else:
                st.success(f"✅ APPROVE  (amount ${row['Amount'].values[0]:,.2f})")
        with right:
            st.markdown("**Why — top factors (SHAP):**")
            reasons_df = pd.DataFrame(
                [{"feature": f, "toward": "fraud" if v > 0 else "legit", "weight": round(v, 2)}
                 for f, v in reasons]
            )
            st.dataframe(reasons_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
