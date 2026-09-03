"""Pure, versioned decision semantics shared by API and rules code."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Mapping

from .policy import DEFAULT_POLICY, DEFAULT_POLICY_VERSION, TenantPolicy

if TYPE_CHECKING:
    from .rules import RuleHit

DECISION_SCHEMA_VERSION = "1.1"
POLICY_VERSION = DEFAULT_POLICY_VERSION
ENGINE_MODE = "rules-only-shadow"
LEGACY_REVIEW_AT = 0.30
LEGACY_DECLINE_AT = 0.80


class DecisionAction(str, Enum):
    APPROVE = "approve"
    REVIEW = "review"
    DECLINE = "decline"


ACTION_ORDER = {
    DecisionAction.APPROVE.value: 0,
    DecisionAction.REVIEW.value: 1,
    DecisionAction.DECLINE.value: 2,
}


@dataclass(frozen=True)
class DecisionOutcome:
    """Deterministic advisory result; Phase 0 intentionally has no ML score."""

    transaction_id: str
    decision: DecisionAction
    rule_hits: tuple["RuleHit", ...]
    mode: str = ENGINE_MODE
    policy_version: str = POLICY_VERSION
    risk_score: None = None
    model_version: None = None
    enforced: bool = False


def normalize_action(action: str | DecisionAction) -> DecisionAction:
    try:
        return DecisionAction(action)
    except ValueError as exc:
        allowed = ", ".join(ACTION_ORDER)
        raise ValueError(
            f"unknown decision action {action!r}; expected one of: {allowed}"
        ) from exc


def stricter_action(*actions: str | DecisionAction) -> DecisionAction:
    """Return the strictest action using the public v1 action ordering."""

    if not actions:
        raise ValueError("at least one decision action is required")
    normalized = [normalize_action(action) for action in actions]
    return max(normalized, key=lambda action: ACTION_ORDER[action.value])


def legacy_action_for_probability(probability: float) -> DecisionAction:
    """Map the legacy model output to its documented three-zone policy."""

    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("probability must be finite and between 0 and 1")
    if probability >= LEGACY_DECLINE_AT:
        return DecisionAction.DECLINE
    if probability >= LEGACY_REVIEW_AT:
        return DecisionAction.REVIEW
    return DecisionAction.APPROVE


def decide_payment(
    transaction_id: str,
    payment: Mapping[str, object],
    trusted_context: Mapping[str, object] | None = None,
    *,
    policy: TenantPolicy = DEFAULT_POLICY,
    policy_version: str = POLICY_VERSION,
) -> DecisionOutcome:
    """Evaluate a canonical payment using only trusted server-side context."""

    from . import rules

    hits, rules_action = rules.evaluate_canonical(payment, trusted_context, policy)
    return DecisionOutcome(
        transaction_id=transaction_id,
        decision=normalize_action(rules_action),
        rule_hits=tuple(hits),
        policy_version=policy_version,
    )
