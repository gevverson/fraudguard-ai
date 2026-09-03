"""Validated, deterministic tenant policy documents."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_POLICY_VERSION = "rules-v2"
POLICY_SCHEMA_VERSION = "1"
_POLICY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class FrozenThresholds(dict[str, Decimal]):
    """A JSON-serializable mapping that cannot be changed after validation."""

    def _immutable(self, *_args, **_kwargs):
        raise TypeError("policy thresholds are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class TenantPolicy(BaseModel):
    """Rules configuration whose serialized form is safe to version."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    policy_schema_version: Literal["1"] = POLICY_SCHEMA_VERSION
    high_amount_review_thresholds: FrozenThresholds = Field(
        default_factory=lambda: FrozenThresholds(
            {"USD": Decimal("10000.00")}
        )
    )
    velocity_decline_count_60s: int = Field(default=10, ge=1, le=100_000)

    @field_validator("high_amount_review_thresholds", mode="before")
    @classmethod
    def validate_thresholds(cls, value: object) -> FrozenThresholds:
        if not isinstance(value, dict):
            raise ValueError("high-amount thresholds must be a currency mapping")
        normalized: dict[str, Decimal] = {}
        for currency, raw_threshold in value.items():
            if isinstance(raw_threshold, bool):
                raise ValueError("high-amount thresholds must be numeric")
            try:
                threshold = Decimal(str(raw_threshold))
            except Exception as exc:
                raise ValueError("high-amount thresholds must be numeric") from exc
            if _CURRENCY_PATTERN.fullmatch(currency) is None:
                raise ValueError("threshold currencies must be uppercase ISO-style codes")
            if not threshold.is_finite() or threshold <= 0:
                raise ValueError("high-amount thresholds must be finite and positive")
            if threshold.as_tuple().exponent < -2:
                raise ValueError("high-amount thresholds support at most two decimals")
            if threshold > Decimal("9999999999999999.99"):
                raise ValueError("high-amount threshold exceeds supported precision")
            normalized[currency] = threshold.quantize(Decimal("0.01"))
        return FrozenThresholds(sorted(normalized.items()))


DEFAULT_POLICY = TenantPolicy()


def validate_policy_version(value: str) -> str:
    version = value.strip()
    if _POLICY_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(
            "policy version must be 1-64 safe characters and begin with an alphanumeric"
        )
    return version


def policy_document(policy: TenantPolicy) -> dict[str, object]:
    """Return the stable JSON-compatible representation persisted in PostgreSQL."""

    return policy.model_dump(mode="json")


def canonical_policy_json(policy: TenantPolicy) -> str:
    return json.dumps(
        policy_document(policy),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def policy_sha256(policy: TenantPolicy) -> str:
    return hashlib.sha256(canonical_policy_json(policy).encode()).hexdigest()
