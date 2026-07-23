# FraudGuard AI

Real-time **card-fraud detection** engine with an explainable scoring API and a
demo dashboard. Built to show a bank / merchant the problem *and* the fix on one
screen: "here's a suspicious transaction, here's the risk score, here's *why*."

> **Scope & ethics.** This project is **defensive only**. It scores transactions
> and explains risk. It does **not** contain, and will never contain, tooling to
> commit fraud, test stolen cards, or bypass KYC/OTP. All data used is public,
> synthetic, or anonymized (no real PANs/CVVs).

## What it does

- **Detect** — score a transaction 0–100 for fraud risk in real time.
- **Explain** — return the top reasons a transaction was flagged (velocity, amount
  deviation, odd hour, new geo, etc.) so an analyst can act on it.
- **Demonstrate** — a dashboard that plays back the public dataset and shows the
  engine catching fraud, with an ROI view (losses prevented vs. false declines).

## Repo layout

```
fraudguard-ai/
├── data/           # datasets (git-ignored — never committed)
├── src/
│   ├── models/     # training + inference code
│   └── app/        # FastAPI service + Streamlit dashboard
├── notebooks/      # EDA + experiments
├── docs/           # ARCHITECTURE.md, payments notes, compliance
├── tests/
└── requirements.txt
```

## Dataset

Uses the public [MLG-ULB Credit Card Fraud dataset](https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud)
(284,807 transactions, 492 fraud, 0.173%). Features `V1–V28` are anonymized PCA
components; plus `Time`, `Amount`, `Class` (1 = fraud). The file is git-ignored —
place `creditcard.csv` in `data/` yourself.

## Quickstart

```bash
cd ~/fraudguard-ai
source venv/bin/activate
pip install -r requirements.txt

# data already in data/creditcard.csv
python -c "import pandas as pd; print(pd.read_csv('data/creditcard.csv').shape)"
```

## Roadmap

- [x] Project scaffold + dataset loaded
- [ ] Baseline model (XGBoost + class weighting) with PR-AUC / recall reporting
- [ ] SHAP explanations per prediction
- [ ] FastAPI `/score` endpoint (accepts transaction *metadata* only)
- [ ] Streamlit dashboard (live detector + ROI view)
- [ ] Model monitoring / drift detection (see `docs/ARCHITECTURE.md`)

## License

See [LICENSE](LICENSE).
