"""Version 1 request and response contracts for the public decision API."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .decision import (
    DECISION_SCHEMA_VERSION,
    ENGINE_MODE,
    POLICY_VERSION,
    DecisionAction,
)
from .velocity import FEATURE_VERSION, VelocityStatus

TransactionId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
OpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=6,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
CurrencyCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z]{3}$"),
]


class PaymentChannel(str, Enum):
    CARD = "card"
    MOBILE_MONEY = "mobile_money"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"


class TransactionType(str, Enum):
    AUTHORIZATION = "authorization"
    PURCHASE = "purchase"
    TRANSFER = "transfer"
    PAYOUT = "payout"
    REFUND = "refund"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


class TokenizedIdentifiers(BaseModel):
    """Opaque references only; clear account or identity data is invalid."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    payment_method_token: OpaqueId | None = Field(
        default=None,
        description="PSP or vault token for the payment instrument; never a PAN",
    )
    customer_id: OpaqueId | None = None
    account_id: OpaqueId | None = None
    merchant_id: OpaqueId | None = None
    device_id: OpaqueId | None = None
    beneficiary_id: OpaqueId | None = None

    @field_validator("*")
    @classmethod
    def identifiers_must_be_opaque(cls, value: str | None) -> str | None:
        if value is None:
            return value
        compact = value.replace("-", "").replace(" ", "")
        if re.fullmatch(r"\d{10,19}", compact):
            raise ValueError(
                "identifier must be an opaque token, not a clear numeric identifier"
            )
        return value

    @model_validator(mode="after")
    def at_least_one_subject_identifier(self) -> "TokenizedIdentifiers":
        subject_fields = (
            self.payment_method_token,
            self.customer_id,
            self.account_id,
            self.device_id,
            self.beneficiary_id,
        )
        if not any(subject_fields):
            raise ValueError("at least one tokenized subject identifier is required")
        return self


_SENSITIVE_KEYS = {
    "card_number",
    "cardnumber",
    "pan",
    "primary_account_number",
    "cvv",
    "cvc",
    "cid",
    "pin",
    "pin_block",
    "track_data",
    "track1",
    "track2",
}


def _find_sensitive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_").replace(" ", "_")
            if normalized in _SENSITIVE_KEYS:
                found.add(normalized)
            found.update(_find_sensitive_keys(child))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_find_sensitive_keys(child))
        return found
    return set()


class DecisionRequest(BaseModel):
    """Canonical payment event accepted by POST /v1/decisions."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_id: TransactionId
    occurred_at: datetime
    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    currency: CurrencyCode
    channel: PaymentChannel
    transaction_type: TransactionType
    identifiers: TokenizedIdentifiers

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_credentials(cls, value: object) -> object:
        sensitive_keys = _find_sensitive_keys(value)
        if sensitive_keys:
            names = ", ".join(sorted(sensitive_keys))
            raise ValueError(
                f"sensitive payment credential fields are not accepted: {names}"
            )
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def amount_must_be_json_number(cls, value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("amount must be a JSON number")
        return Decimal(str(value))

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def zero_amount_is_authorization_only(self) -> "DecisionRequest":
        if self.amount == 0 and self.transaction_type is not TransactionType.AUTHORIZATION:
            raise ValueError("zero amount is valid only for an authorization")
        return self


class RuleHitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    action: DecisionAction
    reason: str


class DecisionResponse(BaseModel):
    """A durable, non-enforcing recommendation from the shadow service."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = DECISION_SCHEMA_VERSION
    decision_id: UUID
    transaction_id: TransactionId
    mode: Literal["rules-only-shadow"] = ENGINE_MODE
    policy_version: Literal["rules-v2"] = POLICY_VERSION
    feature_version: Literal["velocity-v1"] = FEATURE_VERSION
    feature_status: VelocityStatus
    decision: DecisionAction
    enforced: Literal[False] = False
    risk_score: None = None
    model_version: None = None
    rule_hits: list[RuleHitResponse]
