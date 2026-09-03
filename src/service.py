"""Orchestration for authenticated, durable, idempotent shadow decisions."""

from __future__ import annotations

import json

from .config import Settings
from .contracts import DecisionRequest, DecisionResponse, RuleHitResponse
from .decision import decide_payment
from .persistence import (
    DecisionClaim,
    DecisionFailed,
    DecisionLedger,
    PolicyRepository,
    PolicyUnavailable,
    StoredDecision,
    request_fingerprint,
    subject_fingerprints,
)
from .security import TenantContext
from .velocity import (
    SubjectKind,
    VelocityEvent,
    VelocitySnapshot,
    VelocityStatus,
    VelocityStore,
    VelocityUnavailable,
)

_SUBJECT_FIELDS = {
    "payment_method_token": SubjectKind.PAYMENT_METHOD,
    "customer_id": SubjectKind.CUSTOMER,
    "account_id": SubjectKind.ACCOUNT,
    "device_id": SubjectKind.DEVICE,
    "beneficiary_id": SubjectKind.BENEFICIARY,
}


def _strict_json(value: object) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class DecisionService:
    def __init__(
        self,
        *,
        settings: Settings,
        ledger: DecisionLedger,
        policies: PolicyRepository,
        velocity: VelocityStore,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._policies = policies
        self._velocity = velocity

    def _velocity_event(
        self, payment: DecisionRequest, tenant: TenantContext
    ) -> VelocityEvent:
        identifiers = payment.identifiers.model_dump(mode="json", exclude_none=True)
        subjects = {
            kind: identifiers[field]
            for field, kind in _SUBJECT_FIELDS.items()
            if field in identifiers
        }
        return VelocityEvent(
            tenant_id=str(tenant.tenant_id),
            transaction_id=payment.transaction_id,
            subjects=subjects,
        )

    def _record_failure(
        self,
        claim: DecisionClaim,
        *,
        tenant: TenantContext,
        request_id: str,
        code: str,
        retryable: bool,
        response_json: str | None = None,
    ) -> None:
        self._ledger.fail(
            claim,
            credential_id=tenant.credential_id,
            request_id=request_id,
            failure_code=code,
            retryable=retryable,
            response_json=response_json,
        )

    def decide(
        self,
        payment: DecisionRequest,
        *,
        tenant: TenantContext,
        request_id: str,
    ) -> StoredDecision:
        fingerprint = request_fingerprint(payment, self._settings.fingerprint_key)
        reservation = self._ledger.reserve(
            tenant_id=tenant.tenant_id,
            credential_id=tenant.credential_id,
            payment=payment,
            fingerprint=fingerprint,
            subject_hashes=subject_fingerprints(
                payment, self._settings.fingerprint_key
            ),
            request_id=request_id,
        )
        if isinstance(reservation, StoredDecision):
            return reservation

        try:
            policy = self._policies.get_version(
                tenant.tenant_id, reservation.policy_version
            )
        except PolicyUnavailable:
            self._record_failure(
                reservation,
                tenant=tenant,
                request_id=request_id,
                code="policy_unavailable",
                retryable=True,
            )
            raise

        try:
            velocity = self._velocity.observe(
                self._velocity_event(payment, tenant)
            )
        except VelocityUnavailable:
            if self._settings.velocity_failure_mode == "fail_closed":
                self._record_failure(
                    reservation,
                    tenant=tenant,
                    request_id=request_id,
                    code="velocity_unavailable",
                    retryable=True,
                )
                raise
            velocity = VelocitySnapshot.unavailable()

        trusted_context: dict[str, object] | None = None
        if velocity.status is VelocityStatus.AVAILABLE:
            trusted_context = {"max_count_60s": velocity.max_count_60s}

        try:
            outcome = decide_payment(
                payment.transaction_id,
                payment.model_dump(mode="python"),
                trusted_context,
                policy=policy.policy,
                policy_version=policy.policy_version,
            )
            response = DecisionResponse(
                decision_id=reservation.decision_id,
                transaction_id=outcome.transaction_id,
                policy_version=outcome.policy_version,
                feature_version=velocity.feature_version,
                feature_status=velocity.status,
                decision=outcome.decision,
                rule_hits=[
                    RuleHitResponse(
                        rule=hit.rule,
                        action=hit.action,
                        reason=hit.reason,
                    )
                    for hit in outcome.rule_hits
                ],
            )
            response_document = response.model_dump(mode="json")
            response_json = _strict_json(response_document)
            rule_hits_json = _strict_json(response_document["rule_hits"])
        except Exception as exc:
            failure_json = _strict_json(
                {
                    "decision_id": str(reservation.decision_id),
                    "detail": {
                        "code": "decision_evaluation_failed",
                        "message": "the decision could not be evaluated",
                    },
                }
            )
            self._record_failure(
                reservation,
                tenant=tenant,
                request_id=request_id,
                code="decision_evaluation_failed",
                retryable=False,
                response_json=failure_json,
            )
            raise DecisionFailed(
                decision_id=reservation.decision_id,
                response_json=failure_json,
                replayed=False,
            ) from exc

        return self._ledger.complete(
            reservation,
            credential_id=tenant.credential_id,
            request_id=request_id,
            response_json=response_json,
            action=outcome.decision.value,
            mode=outcome.mode,
            schema_version=response.schema_version,
            feature_version=response.feature_version,
            feature_status=response.feature_status.value,
            model_version=outcome.model_version,
            service_version=self._settings.service_version,
            enforced=outcome.enforced,
            rule_hits_json=rule_hits_json,
        )
