"""Leakage-safe chronological evaluation for fraud classifiers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)


@dataclass(frozen=True)
class EvaluationConfig:
    time_column: str = "Time"
    label_column: str = "Class"
    value_column: str = "Amount"
    train_fraction: float = 0.60
    calibration_fraction: float = 0.20
    target_fpr: float = 0.001
    random_state: int = 42
    bootstrap_samples: int = 200
    confidence_level: float = 0.95
    calibration_bins: int = 10

    def validate(self) -> None:
        if not 0 < self.train_fraction < 1:
            raise ValueError("train_fraction must be between 0 and 1")
        if not 0 < self.calibration_fraction < 1:
            raise ValueError("calibration_fraction must be between 0 and 1")
        if self.train_fraction + self.calibration_fraction >= 1:
            raise ValueError("split fractions must leave a non-empty test split")
        if not 0 <= self.target_fpr <= 1:
            raise ValueError("target_fpr must be between 0 and 1")
        if self.bootstrap_samples < 0:
            raise ValueError("bootstrap_samples cannot be negative")
        if not 0 < self.confidence_level < 1:
            raise ValueError("confidence_level must be between 0 and 1")
        if self.calibration_bins < 2:
            raise ValueError("calibration_bins must be at least 2")


@dataclass(frozen=True)
class ChronologicalSplits:
    train: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SigmoidCalibrator:
    """Serializable Platt calibration over the raw probability logit."""

    coefficient: float
    intercept: float
    clip_epsilon: float = 1e-6

    @classmethod
    def fit(
        cls,
        probabilities: Sequence[float],
        labels: Sequence[int],
        random_state: int = 42,
    ):
        scores = _probabilities(probabilities)
        y_true = _binary_labels(labels, "calibration")
        if len(scores) != len(y_true):
            raise ValueError("calibration labels and probabilities must align")
        logits = _logit(scores, 1e-6).reshape(-1, 1)
        model = LogisticRegression(
            solver="lbfgs",
            max_iter=1000,
            random_state=random_state,
        )
        model.fit(logits, y_true)
        return cls(
            float(model.coef_[0, 0]),
            float(model.intercept_[0]),
        )

    def predict(self, probabilities: Sequence[float]) -> np.ndarray:
        values = (
            self.coefficient
            * _logit(_probabilities(probabilities), self.clip_epsilon)
            + self.intercept
        )
        result = np.empty_like(values)
        positive = values >= 0
        result[positive] = 1 / (1 + np.exp(-values[positive]))
        exponent = np.exp(values[~positive])
        result[~positive] = exponent / (1 + exponent)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "platt_logit",
            "input": "positive_class_predict_proba",
            "coefficient": self.coefficient,
            "intercept": self.intercept,
            "clip_epsilon": self.clip_epsilon,
            "fit_split": "calibration",
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        if payload.get("method") != "platt_logit":
            raise ValueError("unsupported calibration method")
        return cls(
            float(payload["coefficient"]),
            float(payload["intercept"]),
            float(payload["clip_epsilon"]),
        )


@dataclass
class EvaluationResult:
    model: Any
    calibrator: SigmoidCalibrator
    threshold: float
    feature_columns: list[str]
    metrics: dict[str, Any]
    policy: dict[str, Any]
    test_labels: np.ndarray
    test_probabilities: np.ndarray


def make_chronological_splits(
    frame: pd.DataFrame,
    config: EvaluationConfig | None = None,
) -> ChronologicalSplits:
    """Dedupe then split without putting one timestamp in multiple slices."""

    config = config or EvaluationConfig()
    config.validate()
    required = {
        config.time_column,
        config.label_column,
        config.value_column,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")
    if frame.empty or frame[config.time_column].isna().any():
        raise ValueError("dataset must be non-empty and timestamps cannot be missing")
    _label_domain(frame[config.label_column])

    before = len(frame)
    ordered = (
        frame.drop_duplicates()
        .sort_values(config.time_column, kind="mergesort")
        .reset_index(drop=True)
    )
    times = ordered[config.time_column].to_numpy()
    boundaries = np.flatnonzero(times[1:] != times[:-1]) + 1
    if len(boundaries) < 2:
        raise ValueError("at least three distinct timestamps are required")

    wanted_train = len(ordered) * config.train_fraction
    wanted_calibration_end = len(ordered) * (
        config.train_fraction + config.calibration_fraction
    )
    calibration_end = int(
        min(
            boundaries[1:],
            key=lambda cut: (
                abs(cut - wanted_calibration_end),
                int(cut),
            ),
        )
    )
    train_end = int(
        min(
            boundaries[boundaries < calibration_end],
            key=lambda cut: (abs(cut - wanted_train), int(cut)),
        )
    )
    train = ordered.iloc[:train_end].copy()
    calibration = ordered.iloc[train_end:calibration_end].copy()
    test = ordered.iloc[calibration_end:].copy()

    for name, split in (
        ("train", train),
        ("calibration", calibration),
        ("test", test),
    ):
        _binary_labels(split[config.label_column], name)

    metadata = {
        "strategy": "chronological_timestamp_groups",
        "requested_fractions": {
            "train": config.train_fraction,
            "calibration": config.calibration_fraction,
            "test": (
                1 - config.train_fraction - config.calibration_fraction
            ),
        },
        "rows_before_deduplication": before,
        "rows_after_deduplication": len(ordered),
        "duplicates_dropped": before - len(ordered),
        "train": _split_summary(train, config),
        "calibration": _split_summary(calibration, config),
        "test": _split_summary(test, config),
        "boundaries": {
            "train_max_time": _json_safe(
                train[config.time_column].iloc[-1]
            ),
            "calibration_min_time": _json_safe(
                calibration[config.time_column].iloc[0]
            ),
            "calibration_max_time": _json_safe(
                calibration[config.time_column].iloc[-1]
            ),
            "test_min_time": _json_safe(
                test[config.time_column].iloc[0]
            ),
        },
    }
    return ChronologicalSplits(train, calibration, test, metadata)


def select_operating_threshold(
    labels: Sequence[int],
    probabilities: Sequence[float],
    target_fpr: float,
) -> dict[str, Any]:
    """Maximize calibration recall under the false-positive-rate ceiling."""

    if not 0 <= target_fpr <= 1:
        raise ValueError("target_fpr must be between 0 and 1")
    y_true = _binary_labels(labels, "threshold-selection")
    scores = _probabilities(probabilities)
    if len(y_true) != len(scores):
        raise ValueError("labels and probabilities must align")

    order = np.argsort(-scores, kind="mergesort")
    sorted_scores = scores[order]
    sorted_labels = y_true[order]
    ends = np.r_[
        np.flatnonzero(sorted_scores[1:] != sorted_scores[:-1]),
        len(scores) - 1,
    ]
    true_positives = np.cumsum(sorted_labels)[ends]
    false_positives = np.cumsum(1 - sorted_labels)[ends]
    thresholds = np.r_[
        np.nextafter(sorted_scores[0], np.inf),
        sorted_scores[ends],
    ]
    true_positives = np.r_[0, true_positives]
    false_positives = np.r_[0, false_positives]
    recall = true_positives / y_true.sum()
    false_positive_rate = false_positives / (
        len(y_true) - y_true.sum()
    )
    precision = np.divide(
        true_positives,
        true_positives + false_positives,
        out=np.zeros_like(true_positives, dtype=float),
        where=(true_positives + false_positives) > 0,
    )
    feasible = np.flatnonzero(
        false_positive_rate <= target_fpr + np.finfo(float).eps
    )
    best = max(
        feasible,
        key=lambda index: (
            float(recall[index]),
            -float(false_positive_rate[index]),
            float(precision[index]),
            float(thresholds[index]),
        ),
    )
    threshold = float(thresholds[best])
    return {
        "target_fpr": float(target_fpr),
        "threshold": threshold,
        "selection_rule": (
            "maximize recall subject to false_positive_rate <= target_fpr; "
            "break ties by lower FPR, higher precision, then higher threshold"
        ),
        "selected_on": "calibration",
        "calibration_operating_point": operating_point_metrics(
            y_true,
            scores,
            threshold,
        ),
    }


def operating_point_metrics(
    labels,
    probabilities,
    threshold: float,
    transaction_values=None,
) -> dict[str, Any]:
    y_true = _binary_labels(labels, "operating-point")
    scores = _probabilities(probabilities)
    if len(y_true) != len(scores):
        raise ValueError("labels and probabilities must align")
    if not np.isfinite(threshold):
        raise ValueError("threshold must be finite")

    predicted = scores >= threshold
    positive = y_true == 1
    true_positives = int(np.sum(predicted & positive))
    false_positives = int(np.sum(predicted & ~positive))
    false_negatives = int(np.sum(~predicted & positive))
    true_negatives = int(np.sum(~predicted & ~positive))

    caught_value = None
    total_value = None
    value_recall = None
    if transaction_values is not None:
        values = np.asarray(transaction_values, dtype=float)
        if (
            values.ndim != 1
            or len(values) != len(y_true)
            or not np.isfinite(values).all()
            or (values < 0).any()
        ):
            raise ValueError(
                "transaction values must align and be finite/non-negative"
            )
        caught_value = float(values[predicted & positive].sum())
        total_value = float(values[positive].sum())
        value_recall = _ratio(caught_value, total_value)

    return {
        "threshold": float(threshold),
        "recall": _ratio(
            true_positives,
            true_positives + false_negatives,
        ),
        "precision": _ratio(
            true_positives,
            true_positives + false_positives,
        ),
        "false_positive_rate": _ratio(
            false_positives,
            false_positives + true_negatives,
        ),
        "value_weighted_recall": value_recall,
        "fraud_caught": true_positives,
        "fraud_missed": false_negatives,
        "false_alarms": false_positives,
        "legitimate_approved": true_negatives,
        "fraud_value_caught": caught_value,
        "total_fraud_value": total_value,
    }


def expected_calibration_error(
    labels,
    probabilities,
    bins: int = 10,
) -> float:
    if bins < 2:
        raise ValueError("bins must be at least 2")
    y_true = _binary_labels(labels, "calibration-error").astype(float)
    scores = _probabilities(probabilities)
    if len(y_true) != len(scores):
        raise ValueError("labels and probabilities must align")

    bin_ids = np.minimum((scores * bins).astype(int), bins - 1)
    error = 0.0
    for bin_id in range(bins):
        mask = bin_ids == bin_id
        if mask.any():
            error += float(mask.mean()) * abs(
                float(y_true[mask].mean())
                - float(scores[mask].mean())
            )
    return error


def evaluate_predictions(
    labels,
    probabilities,
    threshold: float,
    transaction_values,
    calibration_bins: int = 10,
) -> dict[str, Any]:
    y_true = _binary_labels(labels, "evaluation")
    scores = _probabilities(probabilities)
    if len(y_true) != len(scores):
        raise ValueError("labels and probabilities must align")
    return {
        "transactions": len(y_true),
        "fraud": int(y_true.sum()),
        "pr_auc": float(average_precision_score(y_true, scores)),
        "roc_auc_secondary": float(roc_auc_score(y_true, scores)),
        "brier_score": float(brier_score_loss(y_true, scores)),
        "expected_calibration_error": expected_calibration_error(
            y_true,
            scores,
            calibration_bins,
        ),
        "operating_point": operating_point_metrics(
            y_true,
            scores,
            threshold,
            transaction_values,
        ),
    }


def bootstrap_confidence_intervals(
    labels,
    probabilities,
    threshold: float,
    transaction_values,
    samples: int = 200,
    confidence_level: float = 0.95,
    random_state: int = 42,
    calibration_bins: int = 10,
) -> dict[str, Any]:
    """Return deterministic stratified percentile CIs for the test slice."""

    if samples < 0 or not 0 < confidence_level < 1:
        raise ValueError("invalid bootstrap configuration")
    y_true = _binary_labels(labels, "bootstrap")
    scores = _probabilities(probabilities)
    values = np.asarray(transaction_values, dtype=float)
    if len(y_true) != len(scores) or len(y_true) != len(values):
        raise ValueError("bootstrap inputs must align")

    output = {
        "method": "stratified_percentile_bootstrap",
        "confidence_level": confidence_level,
        "samples": samples,
        "random_state": random_state,
        "intervals": {},
    }
    if not samples:
        return output

    positive = np.flatnonzero(y_true == 1)
    negative = np.flatnonzero(y_true == 0)
    rng = np.random.default_rng(random_state)
    metric_names = (
        "pr_auc",
        "roc_auc_secondary",
        "brier_score",
        "expected_calibration_error",
        "recall",
        "precision",
        "false_positive_rate",
        "value_weighted_recall",
    )
    estimates = {name: [] for name in metric_names}

    for _ in range(samples):
        chosen = np.r_[
            rng.choice(positive, len(positive), replace=True),
            rng.choice(negative, len(negative), replace=True),
        ]
        rng.shuffle(chosen)
        run = evaluate_predictions(
            y_true[chosen],
            scores[chosen],
            threshold,
            values[chosen],
            calibration_bins,
        )
        for name in (
            "pr_auc",
            "roc_auc_secondary",
            "brier_score",
            "expected_calibration_error",
        ):
            estimates[name].append(run[name])
        for name in (
            "recall",
            "precision",
            "false_positive_rate",
            "value_weighted_recall",
        ):
            estimates[name].append(run["operating_point"][name])

    tail = (1 - confidence_level) / 2
    output["intervals"] = {
        name: {
            "lower": float(np.quantile(metric_values, tail)),
            "upper": float(np.quantile(metric_values, 1 - tail)),
        }
        for name, metric_values in estimates.items()
    }
    return output


def run_evaluation_pipeline(
    frame: pd.DataFrame,
    model: Any,
    config: EvaluationConfig | None = None,
    feature_columns: Sequence[str] | None = None,
    model_artifact: str = "models/baseline_xgb.json",
) -> EvaluationResult:
    """Fit train, freeze calibration/policy, then use test outcomes once."""

    config = config or EvaluationConfig()
    splits = make_chronological_splits(frame, config)
    features = list(
        feature_columns
        or [
            column
            for column in splits.train
            if column != config.label_column
        ]
    )
    if (
        not features
        or config.label_column in features
        or len(features) != len(set(features))
    ):
        raise ValueError("features must be unique and exclude the label")
    missing = sorted(set(features) - set(splits.train.columns))
    if missing:
        raise ValueError(f"missing feature columns: {', '.join(missing)}")

    model.fit(
        splits.train[features],
        splits.train[config.label_column].to_numpy(dtype=int),
    )
    calibration_labels = splits.calibration[
        config.label_column
    ].to_numpy(dtype=int)
    raw_calibration = _positive_probability(
        model,
        splits.calibration[features],
    )
    calibrator = SigmoidCalibrator.fit(
        raw_calibration,
        calibration_labels,
        config.random_state,
    )
    calibrated_calibration = calibrator.predict(raw_calibration)
    selection = select_operating_threshold(
        calibration_labels,
        calibrated_calibration,
        config.target_fpr,
    )
    threshold = float(selection["threshold"])

    ordered = pd.concat(
        (splits.train, splits.calibration, splits.test),
        ignore_index=True,
    )
    reproducibility = {
        "random_state": config.random_state,
        "dataset_sha256": dataframe_fingerprint(ordered),
        "feature_order": features,
        "model_class": (
            f"{model.__class__.__module__}.{model.__class__.__qualname__}"
        ),
        "model_parameters": _json_safe(
            model.get_params(deep=False)
            if hasattr(model, "get_params")
            else {}
        ),
        "library_versions": library_versions(),
    }
    policy = {
        "schema_version": "fraudguard.model-policy.v1",
        "model": {
            "artifact": model_artifact,
            "type": reproducibility["model_class"],
            "feature_order": features,
            "positive_class": 1,
        },
        "score_calibration": calibrator.to_dict(),
        "decision_policy": selection,
        "reproducibility": reproducibility,
    }

    # The policy is frozen above. Test outcomes enter only final evaluation.
    raw_test = _positive_probability(model, splits.test[features])
    calibrated_test = calibrator.predict(raw_test)
    test_labels = splits.test[config.label_column].to_numpy(dtype=int)
    test_values = splits.test[config.value_column].to_numpy(dtype=float)
    test_metrics = evaluate_predictions(
        test_labels,
        calibrated_test,
        threshold,
        test_values,
        config.calibration_bins,
    )
    test_metrics["raw_score_brier"] = float(
        brier_score_loss(test_labels, raw_test)
    )
    calibration_metrics = evaluate_predictions(
        calibration_labels,
        calibrated_calibration,
        threshold,
        splits.calibration[config.value_column].to_numpy(dtype=float),
        config.calibration_bins,
    )
    calibration_metrics["raw_score_brier"] = float(
        brier_score_loss(calibration_labels, raw_calibration)
    )
    metrics = {
        "schema_version": "fraudguard.evaluation.v1",
        "headline_metric": "pr_auc",
        "splits": splits.metadata,
        "policy_selection": selection,
        "calibration": calibration_metrics,
        "test": test_metrics,
        "test_uncertainty": bootstrap_confidence_intervals(
            test_labels,
            calibrated_test,
            threshold,
            test_values,
            config.bootstrap_samples,
            config.confidence_level,
            config.random_state,
            config.calibration_bins,
        ),
        "reproducibility": reproducibility,
    }
    return EvaluationResult(
        model=model,
        calibrator=calibrator,
        threshold=threshold,
        feature_columns=features,
        metrics=metrics,
        policy=policy,
        test_labels=test_labels,
        test_probabilities=calibrated_test,
    )


def dataframe_fingerprint(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    schema = [(str(column), str(dtype)) for column, dtype in frame.dtypes.items()]
    digest.update(json.dumps(schema, separators=(",", ":")).encode())
    digest.update(
        pd.util.hash_pandas_object(frame, index=False).values.tobytes()
    )
    return digest.hexdigest()


def library_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("numpy", "pandas", "scikit-learn", "xgboost"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _positive_probability(model, features) -> np.ndarray:
    result = np.asarray(model.predict_proba(features), dtype=float)
    if result.ndim != 2 or result.shape != (len(features), 2):
        raise ValueError("predict_proba must return shape (n_rows, 2)")
    if hasattr(model, "classes_"):
        classes = np.asarray(model.classes_)
        positive_columns = np.flatnonzero(classes == 1)
        if len(positive_columns) != 1:
            raise ValueError("model classes must contain positive class 1 exactly once")
        return _probabilities(result[:, int(positive_columns[0])])
    return _probabilities(result[:, 1])


def _label_domain(labels) -> None:
    values = pd.Series(labels)
    if (
        values.isna().any()
        or not set(values.unique()).issubset({0, 1, False, True})
    ):
        raise ValueError("labels must contain only 0 and 1 with no missing values")


def _binary_labels(labels, name: str) -> np.ndarray:
    _label_domain(labels)
    result = np.asarray(labels, dtype=int)
    if result.ndim != 1:
        raise ValueError("labels must be one-dimensional")
    observed = np.unique(result)
    if not np.array_equal(observed, np.array([0, 1])):
        raise ValueError(
            f"{name} split must contain both classes 0 and 1; "
            f"observed {observed.tolist()}"
        )
    return result


def _probabilities(probabilities) -> np.ndarray:
    result = np.asarray(probabilities, dtype=float)
    if (
        result.ndim != 1
        or not len(result)
        or not np.isfinite(result).all()
        or (result < 0).any()
        or (result > 1).any()
    ):
        raise ValueError(
            "probabilities must be a non-empty finite 1D array in [0, 1]"
        )
    return result


def _logit(probabilities, epsilon):
    clipped = np.clip(probabilities, epsilon, 1 - epsilon)
    return np.log(clipped / (1 - clipped))


def _split_summary(split, config):
    fraud = int(split[config.label_column].sum())
    return {
        "rows": len(split),
        "legitimate": len(split) - fraud,
        "fraud": fraud,
        "fraud_rate": fraud / len(split),
        "time_start": _json_safe(split[config.time_column].iloc[0]),
        "time_end": _json_safe(split[config.time_column].iloc[-1]),
    }


def _ratio(numerator, denominator) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)
