"""
rules.py — the deterministic RULES layer (Pillar B1).

Why this exists: an ML model is a photo of yesterday's fraud and drifts silently
as tactics change. Rules don't drift — they encode facts that are ALWAYS true
(a card tested 50x in 10 seconds is fraud, forever). Running rules alongside the
model gives a floor the model can never fall below.

Design (so it lasts):
  - Each rule is one small function: (tx, context) -> RuleHit | None.
  - Adding a rule = adding one function to RULES. No rewrites, ever.
  - Rules return an ACTION ("review" or "decline") + a human reason.
  - The final decision = the STRICTER of (what rules say, what the ML says).

The legacy demo still accepts caller-supplied context for compatibility. The v1
API deliberately does not: velocity must eventually be computed from trusted,
server-side history.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

try:
    from .decision import ACTION_ORDER, stricter_action
except ImportError:  # direct legacy scripts import rules from src/
    from decision import ACTION_ORDER, stricter_action

# Backward-compatible alias; source of truth is decision.py.
ORDER = ACTION_ORDER


@dataclass(frozen=True)
class RuleHit:
    rule: str        # machine name
    action: str      # "review" or "decline"
    reason: str      # human-readable why


# --- Rules that work on THIS data (Amount only) ------------------------------
def zero_amount_auth(tx, ctx) -> Optional[RuleHit]:
    if tx["Amount"] == 0:
        return RuleHit("zero_amount_auth", "review",
                       "$0 authorization — classic card-validation probe")
    return None


def high_amount(tx, ctx) -> Optional[RuleHit]:
    if tx["Amount"] >= 10_000:
        return RuleHit("high_amount", "review",
                       f"Unusually large amount ({tx['Amount']:,.0f} legacy units) "
                       "— verify before approving")
    return None


# --- Velocity rules: inert until `context` carries per-card history -----------
def card_testing_burst(tx, ctx) -> Optional[RuleHit]:
    # The REAL card-testing signal: several tiny charges on one card, fast.
    # A single small charge is normal, so this needs velocity context to fire.
    n = ctx.get("tx_count_last_60s")
    if tx["Amount"] < 2.0 and n is not None and n >= 3:
        return RuleHit("card_testing_burst", "decline",
                       f"{n} tiny charges (${tx['Amount']:.2f}) on this card in 60s — card testing")
    return None


def velocity_burst(tx, ctx) -> Optional[RuleHit]:
    n = ctx.get("tx_count_last_60s")
    if n is not None and n >= 10:
        return RuleHit("velocity_burst", "decline",
                       f"{n} transactions on this card in 60s — card-testing burst")
    return None


def impossible_travel(tx, ctx) -> Optional[RuleHit]:
    kmh = ctx.get("implied_speed_kmh")
    if kmh is not None and kmh > 1000:
        return RuleHit("impossible_travel", "decline",
                       f"Implied travel {kmh:,.0f} km/h between transactions — physically impossible")
    return None


# The registry. Add a rule here and it's live everywhere.
RULES: list[Callable] = [
    zero_amount_auth,
    high_amount,
    card_testing_burst,
    velocity_burst,
    impossible_travel,
]

# Never convert implicitly: support a currency only after validating its threshold.
CANONICAL_HIGH_AMOUNT_THRESHOLDS = {"USD": Decimal("10000.00")}


def zero_amount_card_authorization(tx, _ctx=None) -> Optional[RuleHit]:
    amount = Decimal(str(tx["amount"]))
    if (
        amount == 0
        and tx["channel"] == "card"
        and tx["transaction_type"] == "authorization"
    ):
        return RuleHit(
            "zero_amount_card_authorization",
            "review",
            "Zero-amount card authorization requires verification in shadow mode",
        )
    return None


def canonical_high_amount(tx, _ctx=None) -> Optional[RuleHit]:
    currency = str(tx["currency"])
    threshold = CANONICAL_HIGH_AMOUNT_THRESHOLDS.get(currency)
    if threshold is None:
        return None
    amount = Decimal(str(tx["amount"]))
    if amount >= threshold:
        return RuleHit(
            "high_amount_supported_currency",
            "review",
            f"Amount {currency} {amount:,.2f} meets the "
            f"{currency} {threshold:,.2f} review threshold",
        )
    return None


def canonical_velocity_burst(tx, ctx=None) -> Optional[RuleHit]:
    """Use only server-computed counts supplied by the decision service."""

    count = (ctx or {}).get("max_count_60s")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 10:
        return RuleHit(
            "trusted_velocity_burst",
            "decline",
            f"{count} transactions share a tokenized subject within 60 seconds",
        )
    return None


CANONICAL_RULES: tuple[Callable, ...] = (
    zero_amount_card_authorization,
    canonical_high_amount,
    canonical_velocity_burst,
)


def evaluate_canonical(tx, trusted_context=None):
    """Run canonical rules with context computed inside the trusted boundary."""

    hits = [
        hit
        for rule in CANONICAL_RULES
        if (hit := rule(tx, trusted_context)) is not None
    ]
    action = stricter_action("approve", *(hit.action for hit in hits)).value
    return hits, action


def evaluate(tx: dict, context: Optional[dict] = None):
    """Run every rule. Return (hits, rules_action)."""
    ctx = context or {}
    hits = [hit for rule in RULES if (hit := rule(tx, ctx)) is not None]
    action = "approve"
    for h in hits:
        if ORDER[h.action] > ORDER[action]:
            action = h.action
    return hits, action


def stricter(a: str, b: str) -> str:
    """Return whichever decision is stricter (the safety floor combiner)."""
    return stricter_action(a, b).value


def combine(ml_decision: str, tx: dict, context: Optional[dict] = None):
    """Final decision = stricter of ML and rules. Returns (final, hits)."""
    hits, rules_action = evaluate(tx, context)
    return stricter(ml_decision, rules_action), hits
