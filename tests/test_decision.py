import math

import pytest

from src.decision import (
    DecisionAction,
    decide_payment,
    legacy_action_for_probability,
    stricter_action,
)


def test_action_ordering_is_centralized_and_deterministic():
    assert stricter_action("approve", "review") is DecisionAction.REVIEW
    assert (
        stricter_action("decline", "review", "approve")
        is DecisionAction.DECLINE
    )
    with pytest.raises(ValueError):
        stricter_action("block")
    with pytest.raises(ValueError):
        stricter_action()


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (0.0, DecisionAction.APPROVE),
        (0.2999, DecisionAction.APPROVE),
        (0.30, DecisionAction.REVIEW),
        (0.7999, DecisionAction.REVIEW),
        (0.80, DecisionAction.DECLINE),
        (1.0, DecisionAction.DECLINE),
    ],
)
def test_legacy_probability_boundaries(probability, expected):
    assert legacy_action_for_probability(probability) is expected


@pytest.mark.parametrize("probability", [-0.1, 1.1, math.inf, math.nan])
def test_invalid_legacy_probabilities_are_rejected(probability):
    with pytest.raises(ValueError):
        legacy_action_for_probability(probability)


def test_canonical_decision_does_not_invent_score_or_enforcement():
    payment = {
        "amount": 10000,
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
    }
    result = decide_payment("txn_123456", payment)
    assert result.decision is DecisionAction.APPROVE
    assert result.mode == "rules-only-shadow"
    assert result.policy_version == "rules-v2"
    assert result.risk_score is None
    assert result.model_version is None
    assert result.enforced is False
