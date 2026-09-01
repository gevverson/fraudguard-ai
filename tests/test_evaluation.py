"""Dataset-independent tests for leakage-safe model evaluation."""

import unittest

import numpy as np
import pandas as pd

from src.models.evaluation import (
    EvaluationConfig,
    SigmoidCalibrator,
    bootstrap_confidence_intervals,
    evaluate_predictions,
    expected_calibration_error,
    make_chronological_splits,
    operating_point_metrics,
    run_evaluation_pipeline,
    select_operating_threshold,
)


class ScorePassthroughModel:
    def __init__(self):
        self.fit_times = None

    def fit(self, features, labels):
        self.fit_times = features["Time"].to_numpy(copy=True)
        self.fit_labels = np.asarray(labels).copy()
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, features):
        positive = features["score"].to_numpy(dtype=float)
        return np.column_stack((1 - positive, positive))

    def get_params(self, deep=False):
        return {
            "kind": "score_passthrough",
            "array_parameter": np.array([1, 2]),
            "scalar_parameter": np.int64(7),
            "nonfinite_parameter": np.nan,
        }


class InvalidShapeModel(ScorePassthroughModel):
    def predict_proba(self, features):
        return features["score"].to_numpy(dtype=float)


class InvalidClassesModel(ScorePassthroughModel):
    def fit(self, features, labels):
        super().fit(features, labels)
        self.classes_ = np.array([0, 2])
        return self


def synthetic_frame(test_labels=None):
    rows = 60
    labels = np.tile([0, 1], rows // 2)
    if test_labels is not None:
        labels[-12:] = np.asarray(test_labels)
    scores = np.tile(
        [0.05, 0.80, 0.10, 0.65, 0.20, 0.90],
        rows // 6,
    )
    return pd.DataFrame(
        {
            "Time": np.arange(rows),
            "score": scores,
            "Amount": np.linspace(1, 100, rows),
            "Class": labels,
        }
    )


class ChronologicalSplitTests(unittest.TestCase):
    def test_keeps_equal_timestamps_in_one_split(self):
        frame = synthetic_frame()
        frame["Time"] = np.repeat(np.arange(30), 2)
        config = EvaluationConfig(
            train_fraction=0.5,
            calibration_fraction=0.25,
            bootstrap_samples=0,
        )
        splits = make_chronological_splits(frame, config)
        self.assertLess(
            splits.train["Time"].max(),
            splits.calibration["Time"].min(),
        )
        self.assertLess(
            splits.calibration["Time"].max(),
            splits.test["Time"].min(),
        )
        self.assertEqual(
            len(splits.train)
            + len(splits.calibration)
            + len(splits.test),
            len(frame),
        )
        self.assertEqual(
            set(splits.metadata["boundaries"]),
            {
                "train_max_time",
                "calibration_min_time",
                "calibration_max_time",
                "test_min_time",
            },
        )

    def test_fails_when_a_split_lacks_one_class(self):
        frame = synthetic_frame()
        frame.loc[:35, "Class"] = 0
        with self.assertRaisesRegex(
            ValueError,
            "train split must contain both classes",
        ):
            make_chronological_splits(
                frame,
                EvaluationConfig(bootstrap_samples=0),
            )


class PolicyIsolationTests(unittest.TestCase):
    def test_test_outcomes_cannot_change_calibration_threshold(self):
        config = EvaluationConfig(
            bootstrap_samples=0,
            target_fpr=0.20,
        )
        first_model = ScorePassthroughModel()
        first = run_evaluation_pipeline(
            synthetic_frame([0, 1] * 6),
            first_model,
            config,
            feature_columns=["Time", "score", "Amount"],
        )
        second_model = ScorePassthroughModel()
        second = run_evaluation_pipeline(
            synthetic_frame([1, 0] * 6),
            second_model,
            config,
            feature_columns=["Time", "score", "Amount"],
        )
        self.assertEqual(first.threshold, second.threshold)
        self.assertEqual(
            first.policy["decision_policy"],
            second.policy["decision_policy"],
        )
        self.assertEqual(len(first_model.fit_times), 36)
        self.assertLess(first_model.fit_times.max(), 36)
        self.assertEqual(
            first.policy["decision_policy"]["selected_on"],
            "calibration",
        )
        self.assertIsNone(
            first.policy["reproducibility"]["model_parameters"][
                "nonfinite_parameter"
            ]
        )
        self.assertNotEqual(
            first.metrics["test"]["operating_point"]["recall"],
            second.metrics["test"]["operating_point"]["recall"],
        )

    def test_serialized_calibrator_reproduces_scores(self):
        raw = np.array([0.01, 0.10, 0.30, 0.70, 0.90, 0.99])
        labels = np.array([0, 0, 0, 1, 1, 1])
        calibrator = SigmoidCalibrator.fit(raw, labels)
        restored = SigmoidCalibrator.from_dict(calibrator.to_dict())
        np.testing.assert_allclose(
            restored.predict(raw),
            calibrator.predict(raw),
        )


class MetricTests(unittest.TestCase):
    def test_threshold_maximizes_recall_within_fpr_ceiling(self):
        labels = np.array([0, 0, 0, 0, 1, 1, 1])
        scores = np.array([0.95, 0.40, 0.30, 0.10, 0.90, 0.80, 0.20])
        selection = select_operating_threshold(
            labels,
            scores,
            target_fpr=0.25,
        )
        point = selection["calibration_operating_point"]
        self.assertLessEqual(point["false_positive_rate"], 0.25)
        self.assertAlmostEqual(point["recall"], 2 / 3)
        self.assertAlmostEqual(selection["threshold"], 0.80)

    def test_bootstrap_intervals_are_deterministic(self):
        frame = synthetic_frame()
        arguments = {
            "labels": frame["Class"].to_numpy(),
            "probabilities": frame["score"].to_numpy(),
            "threshold": 0.50,
            "transaction_values": frame["Amount"].to_numpy(),
            "samples": 25,
            "confidence_level": 0.90,
            "random_state": 7,
        }
        first = bootstrap_confidence_intervals(**arguments)
        second = bootstrap_confidence_intervals(**arguments)
        self.assertEqual(first, second)
        self.assertIn(
            "value_weighted_recall",
            first["intervals"],
        )
        self.assertIn("brier_score", first["intervals"])


class ValidationTests(unittest.TestCase):
    def test_config_rejects_invalid_boundaries(self):
        invalid_configs = (
            EvaluationConfig(train_fraction=0),
            EvaluationConfig(calibration_fraction=0),
            EvaluationConfig(
                train_fraction=0.8,
                calibration_fraction=0.2,
            ),
            EvaluationConfig(target_fpr=-0.1),
            EvaluationConfig(bootstrap_samples=-1),
            EvaluationConfig(confidence_level=1),
            EvaluationConfig(calibration_bins=1),
        )
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                config.validate()

    def test_split_rejects_missing_empty_and_invalid_data(self):
        frame = synthetic_frame()
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            make_chronological_splits(frame.drop(columns=["Amount"]))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            make_chronological_splits(frame.iloc[:0])

        too_few_timestamps = frame.copy()
        too_few_timestamps["Time"] = 1
        with self.assertRaisesRegex(ValueError, "distinct timestamps"):
            make_chronological_splits(too_few_timestamps)

        invalid_labels = frame.copy()
        invalid_labels.loc[0, "Class"] = 2
        with self.assertRaisesRegex(ValueError, "only 0 and 1"):
            make_chronological_splits(invalid_labels)

    def test_calibrator_rejects_misaligned_or_unknown_serialization(self):
        with self.assertRaisesRegex(ValueError, "must align"):
            SigmoidCalibrator.fit([0.1], [0, 1])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            SigmoidCalibrator.from_dict({"method": "isotonic"})

    def test_threshold_and_metric_inputs_are_validated(self):
        labels = np.array([0, 0, 1, 1])
        scores = np.array([0.1, 0.2, 0.8, 0.9])

        with self.assertRaisesRegex(ValueError, "target_fpr"):
            select_operating_threshold(labels, scores, -0.1)
        with self.assertRaisesRegex(ValueError, "must align"):
            select_operating_threshold(labels, scores[:-1], 0.1)
        with self.assertRaisesRegex(ValueError, "finite"):
            select_operating_threshold(
                labels,
                np.array([0.1, 0.2, np.nan, 0.9]),
                0.1,
            )
        with self.assertRaisesRegex(ValueError, "threshold must be finite"):
            operating_point_metrics(labels, scores, np.inf)
        with self.assertRaisesRegex(ValueError, "must align"):
            operating_point_metrics(labels, scores[:-1], 0.5)
        with self.assertRaisesRegex(ValueError, "finite/non-negative"):
            operating_point_metrics(
                labels,
                scores,
                0.5,
                [1, 1, -1, 1],
            )
        with self.assertRaisesRegex(ValueError, "at least 2"):
            expected_calibration_error(labels, scores, bins=1)
        with self.assertRaisesRegex(ValueError, "must align"):
            evaluate_predictions(
                labels,
                scores[:-1],
                0.5,
                [1, 1, 1, 1],
            )

    def test_bootstrap_and_pipeline_inputs_are_validated(self):
        frame = synthetic_frame()
        labels = frame["Class"].to_numpy()
        scores = frame["score"].to_numpy()
        values = frame["Amount"].to_numpy()

        with self.assertRaisesRegex(ValueError, "invalid bootstrap"):
            bootstrap_confidence_intervals(
                labels,
                scores,
                0.5,
                values,
                samples=-1,
            )
        with self.assertRaisesRegex(ValueError, "must align"):
            bootstrap_confidence_intervals(
                labels,
                scores,
                0.5,
                values[:-1],
            )
        with self.assertRaisesRegex(ValueError, "features must be unique"):
            run_evaluation_pipeline(
                frame,
                ScorePassthroughModel(),
                EvaluationConfig(bootstrap_samples=0),
                feature_columns=["Time", "Time"],
            )
        with self.assertRaisesRegex(ValueError, "missing feature columns"):
            run_evaluation_pipeline(
                frame,
                ScorePassthroughModel(),
                EvaluationConfig(bootstrap_samples=0),
                feature_columns=["Time", "missing"],
            )
        with self.assertRaisesRegex(ValueError, "shape"):
            run_evaluation_pipeline(
                frame,
                InvalidShapeModel(),
                EvaluationConfig(bootstrap_samples=0),
                feature_columns=["Time", "score", "Amount"],
            )
        with self.assertRaisesRegex(ValueError, "positive class"):
            run_evaluation_pipeline(
                frame,
                InvalidClassesModel(),
                EvaluationConfig(bootstrap_samples=0),
                feature_columns=["Time", "score", "Amount"],
            )


if __name__ == "__main__":
    unittest.main()
