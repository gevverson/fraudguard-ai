"""Train FraudGuard with train/calibration/test chronological isolation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from sklearn.metrics import precision_recall_curve
from xgboost import XGBClassifier

try:
    from src.models.evaluation import (
        EvaluationConfig,
        make_chronological_splits,
        run_evaluation_pipeline,
    )
except ModuleNotFoundError:
    from evaluation import (
        EvaluationConfig,
        make_chronological_splits,
        run_evaluation_pipeline,
    )

DEFAULT_DATA = "data/creditcard.csv"
DEFAULT_MODEL_OUT = "models/baseline_xgb.json"
DEFAULT_POLICY_OUT = "models/baseline_policy.json"
DEFAULT_METRICS_OUT = "reports/metrics.json"
DEFAULT_FIGURE_OUT = "reports/figures/pr_curve.png"


def build_model(
    labels: pd.Series,
    random_state: int = 42,
) -> XGBClassifier:
    """Build a deterministic baseline using train-only imbalance weight."""

    positives = int((labels == 1).sum())
    negatives = int((labels == 0).sum())
    if not positives or not negatives:
        raise ValueError("training labels must contain both classes")
    return XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        scale_pos_weight=negatives / positives,
        eval_metric="aucpr",
        tree_method="hist",
        n_jobs=-1,
        random_state=random_state,
    )


def train_from_frame(
    frame: pd.DataFrame,
    config: EvaluationConfig,
    model_artifact: str = DEFAULT_MODEL_OUT,
):
    """Fit and evaluate without using calibration/test outcomes for training."""

    train_labels = make_chronological_splits(
        frame,
        config,
    ).train[config.label_column]
    model = build_model(train_labels, config.random_state)
    return run_evaluation_pipeline(
        frame,
        model,
        config,
        model_artifact=model_artifact,
    )


def save_outputs(
    result,
    model_out: Path,
    policy_out: Path,
    metrics_out: Path,
) -> None:
    for destination in (model_out, policy_out, metrics_out):
        destination.parent.mkdir(parents=True, exist_ok=True)
    result.model.save_model(str(model_out))
    model_hash = hashlib.sha256(model_out.read_bytes()).hexdigest()
    result.policy["model"]["sha256"] = model_hash
    result.metrics["reproducibility"]["model_artifact_sha256"] = model_hash
    policy_out.write_text(
        json.dumps(result.policy, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    metrics_out.write_text(
        json.dumps(result.metrics, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def save_pr_curve(result, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    precision, recall, _ = precision_recall_curve(
        result.test_labels,
        result.test_probabilities,
    )
    plt.figure(figsize=(6, 4))
    plt.plot(recall, precision, color="#e76f51")
    plt.xlabel("Recall (fraud caught)")
    plt.ylabel("Precision (flags that were right)")
    plt.title(
        "Untouched test PR curve — "
        f"PR-AUC = {result.metrics['test']['pr_auc']:.3f}"
    )
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(destination, dpi=110)
    plt.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--model-out", default=DEFAULT_MODEL_OUT)
    parser.add_argument("--policy-out", default=DEFAULT_POLICY_OUT)
    parser.add_argument("--metrics-out", default=DEFAULT_METRICS_OUT)
    parser.add_argument("--figure-out", default=DEFAULT_FIGURE_OUT)
    parser.add_argument("--target-fpr", type=float, default=0.001)
    parser.add_argument("--train-fraction", type=float, default=0.60)
    parser.add_argument(
        "--calibration-fraction",
        type=float,
        default=0.20,
    )
    parser.add_argument("--bootstrap-samples", type=int, default=200)
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = EvaluationConfig(
        train_fraction=args.train_fraction,
        calibration_fraction=args.calibration_fraction,
        target_fpr=args.target_fpr,
        bootstrap_samples=args.bootstrap_samples,
        random_state=args.random_state,
    )
    result = train_from_frame(
        pd.read_csv(args.data),
        config,
        args.model_out,
    )
    save_outputs(
        result,
        Path(args.model_out),
        Path(args.policy_out),
        Path(args.metrics_out),
    )
    save_pr_curve(result, Path(args.figure_out))

    test = result.metrics["test"]
    splits = result.metrics["splits"]
    point = test["operating_point"]
    print("\n=== UNTOUCHED FUTURE/TEST RESULTS ===")
    print(
        f"Rows: train {splits['train']['rows']:,} | "
        f"calibration {splits['calibration']['rows']:,} | "
        f"test {splits['test']['rows']:,}"
    )
    print(f"PR-AUC: {test['pr_auc']:.4f}")
    print(f"Brier score: {test['brier_score']:.6f} (lower is better)")
    print(
        f"At calibration-selected threshold {result.threshold:.6f} "
        f"for target FPR <= {config.target_fpr:.4%}:"
    )
    print(f"  recall: {point['recall']:.2%}")
    print(f"  precision: {point['precision']:.2%}")
    print(f"  false-positive rate: {point['false_positive_rate']:.4%}")
    print(f"  value-weighted recall: {point['value_weighted_recall']:.2%}")
    print(f"Saved model: {args.model_out}")
    print(f"Saved deployable policy: {args.policy_out}")
    print(f"Saved metrics: {args.metrics_out}")


if __name__ == "__main__":
    main()
