# Repository Guidelines

## Project Structure & Module Organization

FraudGuard AI is a Python fraud-decisioning prototype. Keep reusable logic in
`src/`: `contracts.py` defines the public API schema, `decision.py` and
`rules.py` own versioned decision semantics, `src/app/` contains FastAPI and
Streamlit interfaces, and `src/models/` contains training, evaluation,
calibration, and explainability code. Put tests in `tests/`, architecture notes
in `docs/`, committed evidence in `reports/`, and registry metadata in
`models/registry/`. Datasets and generated model artifacts remain untracked.

## Setup, Training, and Development Commands

Run commands from the repository root because scripts use relative paths.

~~~bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
venv/bin/python -m pytest
venv/bin/python -m ruff check src tests
venv/bin/python src/models/train.py
venv/bin/uvicorn src.app.api:app --reload --port 8000
venv/bin/streamlit run src/app/dashboard.py
~~~

Training and the dashboard require `data/creditcard.csv`. The canonical API and
unit tests must start without a local model artifact.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase`
classes, and `UPPER_CASE` constants. Prefer pure, typed functions and concise
operational docstrings. Preserve deterministic random seeds. Keep rules, model
policy, and product interfaces separate; do not duplicate thresholds in API or
dashboard code. Ruff configuration lives in `pyproject.toml`.

## Testing Guidelines

Use pytest files named `test_<module>.py` and tests named `test_<behavior>()`.
Cover happy paths, validation failures, decision boundaries, temporal leakage,
currency handling, and legacy compatibility. Tests must not require the
git-ignored dataset or model binaries. CI enforces syntax checks, Ruff, all
tests, and at least 85% branch coverage across `src.decision` and
`src.models.evaluation`.

## Commit & Pull Request Guidelines

Use focused, imperative subjects such as `Add canonical decision contract`;
roadmap prefixes such as `B2:` are acceptable. Pull requests should summarize
behavior, list verification commands, link issues, and include screenshots for
dashboard work or sample payloads for API work. Model changes must report
PR-AUC, recall, precision, false-positive rate, calibration, value-weighted
recall, confidence intervals, and the split strategy.

## Security & Artifacts

Never commit real customer/card data, PANs/CVVs, PIN data, secrets, dataset CSVs,
or generated models. Accept opaque tokens only. Do not claim tokenization alone
removes PCI DSS or regulatory scope. Keep new integrations advisory until
authentication, tenant isolation, durable audit logging, and human-review
controls are implemented.
