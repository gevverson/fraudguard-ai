# Repository Guidelines

## Project Structure & Module Organization

FraudGuard AI is a Python fraud-scoring service. Keep reusable decision logic in `src/`: `src/app/` contains the FastAPI API and Streamlit dashboard, `src/models/` contains training, feature engineering, threshold tuning, and SHAP utilities, while `src/rules.py`, `src/registry.py`, and `src/monitor.py` implement the rules floor, model registry, and drift checks. Experiments belong in `notebooks/`, design notes in `docs/`, generated metrics and figures in `reports/`, and version metadata in `models/registry/`. `tests/` is currently a placeholder. Local datasets belong in `data/` and must remain untracked.

## Setup, Training, and Development Commands

Run commands from the repository root because scripts use relative paths.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
venv/bin/python src/models/train_baseline.py
venv/bin/python src/register_model.py --notes "describe the run"
venv/bin/uvicorn src.app.api:app --reload --port 8000
venv/bin/streamlit run src/app/dashboard.py
```

Place the public MLG-ULB dataset at `data/creditcard.csv` before training or launching the dashboard. The API documentation is available at `http://localhost:8000/docs` while the server runs.

## Coding Style & Naming Conventions

Follow the existing PEP 8-style Python: four-space indentation, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_CASE` constants. Prefer small, testable functions and concise docstrings that explain operational intent. Preserve deterministic seeds (`RANDOM_STATE = 42`) in experiments. Keep fraud logic in the rules/model layers rather than duplicating it in API or dashboard code. No formatter or linter is currently configured; group standard-library, third-party, and local imports consistently.

## Testing Guidelines

There is no committed test framework or coverage threshold yet. New behavior should include pytest-style tests under `tests/`, named `test_<module>.py`, with functions named `test_<behavior>()`. Prioritize pure rules, PSI boundaries, registry behavior, threshold decisions, and API validation. Add `pytest` to development dependencies when introducing the suite, then run `venv/bin/python -m pytest`. For a lightweight syntax check, run `venv/bin/python -m compileall src`.

## Commit & Pull Request Guidelines

History uses concise, imperative subjects such as `Add SHAP explainability` and optional roadmap prefixes such as `B2:`. Keep each commit focused and describe the user-visible outcome. Pull requests should summarize the change, list verification commands, link relevant issues, and report recall, precision, and PR-AUC changes for model work. Include screenshots for dashboard changes and example request/response payloads for API changes.

## Security & Artifacts

Never commit real card data, PANs/CVVs, `.env` files, keys, dataset CSVs, or generated `.joblib` models. Use only public, anonymized, or synthetic transaction data, and commit registry metadata only when it accurately describes a reproducible training run.
