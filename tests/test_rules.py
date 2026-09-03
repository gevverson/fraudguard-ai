from decimal import Decimal

import pytest

from src import rules


def canonical_payment(**overrides):
    payment = {
        "amount": Decimal("25.00"),
        "currency": "KES",
        "channel": "mobile_money",
        "transaction_type": "transfer",
    }
    payment.update(overrides)
    return payment


def test_canonical_kes_does_not_use_usd_threshold():
    hits, action = rules.evaluate_canonical(
        canonical_payment(amount=Decimal("10000.00"))
    )
    assert hits == []
    assert action == "approve"


def test_canonical_supported_currency_threshold_is_explicit():
    hits, action = rules.evaluate_canonical(
        canonical_payment(amount=Decimal("10000.00"), currency="USD")
    )
    assert action == "review"
    assert [hit.rule for hit in hits] == ["high_amount_supported_currency"]


def test_zero_amount_rule_is_limited_to_card_authorization():
    hits, action = rules.evaluate_canonical(
        canonical_payment(
            amount=0,
            channel="card",
            transaction_type="authorization",
        )
    )
    assert action == "review"
    assert hits[0].rule == "zero_amount_card_authorization"

    hits, action = rules.evaluate_canonical(
        canonical_payment(
            amount=0,
            channel="wallet",
            transaction_type="authorization",
        )
    )
    assert hits == []
    assert action == "approve"


def test_canonical_velocity_uses_only_the_internal_context_shape():
    payment = canonical_payment(tx_count_last_60s=100)
    hits, action = rules.evaluate_canonical(payment)
    assert hits == []
    assert action == "approve"

    hits, action = rules.evaluate_canonical(
        canonical_payment(),
        {"max_count_60s": 10},
    )
    assert action == "decline"
    assert hits[0].rule == "trusted_velocity_burst"


def test_legacy_rule_combiner_remains_compatible():
    decision, hits = rules.combine("approve", {"Amount": 0})
    assert decision == "review"
    assert hits[0].rule == "zero_amount_auth"

    decision, hits = rules.combine(
        "approve",
        {"Amount": 1},
        {"tx_count_last_60s": 10},
    )
    assert decision == "decline"
    assert {hit.rule for hit in hits} == {
        "card_testing_burst",
        "velocity_burst",
    }


def test_stricter_rejects_unknown_action():
    with pytest.raises(ValueError):
        rules.stricter("approve", "block")
