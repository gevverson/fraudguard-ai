"""FraudGuard AI HTTP API for a tenant-aware shadow pilot."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Dict, NamedTuple

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import registry, rules
from ..config import Settings, get_settings
from ..contracts import DecisionRequest, DecisionResponse
from ..decision import (
    ENGINE_MODE,
    LEGACY_DECLINE_AT,
    LEGACY_REVIEW_AT,
    POLICY_VERSION,
    legacy_action_for_probability,
)
from ..ingress import StreamedBodyLimitMiddleware
from ..rate_limit import RateLimitResult, RateLimitUnavailable
from ..persistence import (
    DecisionFailed,
    DecisionInProgress,
    IdempotencyConflict,
    LedgerUnavailable,
    PersistenceError,
)
from ..runtime import Runtime, build_runtime
from ..security import TenantContext
from ..velocity import VelocityUnavailable

FEATURES = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount"]
REVIEW_AT = LEGACY_REVIEW_AT
DECLINE_AT = LEGACY_DECLINE_AT
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_KNOWN_ROUTES = {
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/score",
    "/v1/decisions",
}
logger = logging.getLogger("fraudguard.api")


class Transaction(BaseModel):
    """Deprecated PCA demo request retained for POST /score clients."""

    model_config = ConfigDict(extra="forbid")

    features: Dict[str, float] = Field(
        ..., description="Legacy keys: Time, V1..V28, Amount"
    )
    context: Dict[str, float] = Field(
        default_factory=dict,
        description="Deprecated caller-supplied context used only by the legacy demo",
    )


class LegacyRuntime(NamedTuple):
    model: Any
    explainer: Any | None
    model_version: str


class LegacyModelUnavailable(RuntimeError):
    """Raised when no local artifact can serve the deprecated endpoint."""


@lru_cache(maxsize=1)
def _load_legacy_runtime() -> LegacyRuntime:
    """Load legacy artifacts on first /score call, never during import or health."""

    try:
        model = registry.load()
        model_version = registry.get_champion() or "registry-unversioned"
    except Exception:
        try:
            import joblib

            model = joblib.load("models/baseline_xgb.joblib")
            model_version = "unversioned"
        except Exception as fallback_error:
            raise LegacyModelUnavailable(
                "legacy model artifact is not installed"
            ) from fallback_error
    try:
        import shap

        explainer = shap.TreeExplainer(model)
    except Exception:
        explainer = None
    return LegacyRuntime(model, explainer, model_version)


def _registered_model_version() -> str | None:
    try:
        return registry.get_champion()
    except Exception:
        return None


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    if _REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    from uuid import uuid4

    return str(uuid4())


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path in _KNOWN_ROUTES:
        return route_path
    return request.url.path if request.url.path in _KNOWN_ROUTES else "unmatched"


def _error(
    status_code: int,
    *,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers=headers,
    )


def _rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        headers["Retry-After"] = str(result.retry_after_seconds)
    return headers


def _validation_errors_without_input(
    exc: RequestValidationError,
) -> list[dict[str, object]]:
    """Return useful validation details without reflecting request values."""

    return [
        {
            "type": str(error.get("type", "value_error")),
            "loc": list(error.get("loc", ())),
            "msg": str(error.get("msg", "invalid request")),
        }
        for error in exc.errors()
    ]


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
) -> FastAPI:
    """Build an isolated app so tests and deployments can inject dependencies."""

    resolved_settings = settings or get_settings()
    resolved_runtime = runtime or build_runtime(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        resolved_runtime.close()

    application = FastAPI(
        title="FraudGuard AI",
        description=(
            "Authenticated advisory fraud decisioning for tokenized payment metadata. "
            "Do not send PAN, CVV, PIN, or magnetic-stripe track data."
        ),
        version=resolved_settings.service_version,
        lifespan=lifespan,
    )
    application.add_middleware(
        StreamedBodyLimitMiddleware,
        max_bytes=resolved_settings.request_max_bytes,
    )
    application.state.runtime = resolved_runtime
    require_decision_write = resolved_runtime.verifier.require("decisions:write")
    require_metrics_read = resolved_runtime.verifier.require("metrics:read")
    operator_endpoints_enabled = resolved_settings.environment in {"local", "test"}

    def require_limited_decision(
        request: Request,
        tenant: TenantContext = Depends(require_decision_write),
    ) -> TenantContext:
        try:
            result = resolved_runtime.rate_limiter.consume(str(tenant.tenant_id))
        except RateLimitUnavailable as exc:
            resolved_runtime.metrics.rate_limit_outcomes.labels("unavailable").inc()
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "rate_limit_unavailable",
                    "message": "tenant quota service is temporarily unavailable",
                },
                headers={"Retry-After": "1"},
            ) from exc
        request.state.rate_limit = result
        outcome = "allowed" if result.allowed else "rejected"
        resolved_runtime.metrics.rate_limit_outcomes.labels(outcome).inc()
        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limit_exceeded",
                    "message": "tenant decision quota exceeded",
                },
                headers=_rate_limit_headers(result),
            )
        return tenant

    @application.middleware("http")
    async def operational_boundary(request: Request, call_next):
        request.state.request_id = _request_id(request)
        started = time.perf_counter()
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > resolved_settings.request_max_bytes
            except ValueError:
                too_large = True
            if too_large:
                response = _error(
                    413,
                    code="request_too_large",
                    message="request body exceeds the configured limit",
                )
                response.headers["X-Request-ID"] = request.state.request_id
                resolved_runtime.metrics.observe_request(
                    route=_route_label(request),
                    method=request.method,
                    status_code=413,
                    duration=time.perf_counter() - started,
                )
                return response

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            duration = time.perf_counter() - started
            route = _route_label(request)
            resolved_runtime.metrics.observe_request(
                route=route,
                method=request.method,
                status_code=status_code,
                duration=duration,
            )
            logger.info(
                json.dumps(
                    {
                        "duration_ms": round(duration * 1000, 3),
                        "event": "http_request",
                        "method": request.method,
                        "request_id": request.state.request_id,
                        "route": route,
                        "status_code": status_code,
                    },
                    separators=(",", ":"),
                )
            )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": _validation_errors_without_input(exc)},
        )

    @application.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(_request: Request, _exc: IdempotencyConflict):
        return _error(
            409,
            code="idempotency_conflict",
            message="transaction_id was already used with different request data",
        )

    @application.exception_handler(DecisionInProgress)
    async def decision_in_progress_handler(_request: Request, _exc: DecisionInProgress):
        return _error(
            409,
            code="decision_in_progress",
            message="a matching decision is still being processed",
            headers={"Retry-After": "1"},
        )

    @application.exception_handler(DecisionFailed)
    async def decision_failed_handler(_request: Request, exc: DecisionFailed):
        return Response(
            content=exc.response_json,
            status_code=500,
            media_type="application/json",
            headers={
                "X-Decision-Replayed": "true" if exc.replayed else "false",
            },
        )

    @application.exception_handler(VelocityUnavailable)
    async def velocity_unavailable_handler(_request: Request, _exc: VelocityUnavailable):
        resolved_runtime.metrics.velocity_failures.labels("rejected").inc()
        return _error(
            503,
            code="velocity_unavailable",
            message="trusted velocity state is temporarily unavailable",
            headers={"Retry-After": "1"},
        )

    @application.exception_handler(LedgerUnavailable)
    @application.exception_handler(PersistenceError)
    async def persistence_unavailable_handler(_request: Request, _exc: PersistenceError):
        return _error(
            503,
            code="decision_ledger_unavailable",
            message="the durable decision ledger is temporarily unavailable",
            headers={"Retry-After": "1"},
        )

    @application.get("/health/live")
    def liveness():
        return {
            "status": "ok",
            "service_version": resolved_settings.service_version,
        }

    @application.get("/health")
    def health():
        """Backward-compatible liveness response without dependency calls."""

        return {
            "status": "ok",
            "canonical_engine": ENGINE_MODE,
            "policy_version": POLICY_VERSION,
            "model_version": _registered_model_version(),
            "features_expected": len(FEATURES),
            "legacy_model_loaded": _load_legacy_runtime.cache_info().currsize > 0,
            "service_version": resolved_settings.service_version,
        }

    @application.get("/health/ready")
    def readiness(response: Response):
        database_ready = resolved_runtime.database.ready()
        velocity = resolved_runtime.velocity.readiness()
        ready = database_ready and velocity.ready
        if not ready:
            response.status_code = 503
        return {
            "status": "ready" if ready else "not_ready",
            "database": "ready" if database_ready else "unavailable",
            "velocity": {
                "backend": velocity.backend,
                "code": velocity.code,
                "production_safe": velocity.production_safe,
                "ready": velocity.ready,
            },
        }

    if operator_endpoints_enabled:

        @application.get("/metrics")
        def metrics(
            _tenant: TenantContext = Depends(require_metrics_read),
        ) -> Response:
            """Expose process metrics only on explicitly local/test apps."""

            body, content_type = resolved_runtime.metrics.render()
            return Response(content=body, media_type=content_type)

    @application.post(
        "/v1/decisions",
        response_model=DecisionResponse,
        responses={
            429: {"description": "Tenant decision quota exceeded"},
            409: {"description": "Idempotency conflict or request in progress"},
            503: {"description": "Required decision dependency unavailable"},
        },
    )
    def create_decision(
        payment: DecisionRequest,
        request: Request,
        tenant: TenantContext = Depends(require_limited_decision),
    ) -> Response:
        stored = resolved_runtime.service.decide(
            payment,
            tenant=tenant,
            request_id=request.state.request_id,
        )
        document = json.loads(stored.response_json)
        resolved_runtime.metrics.observe_decision(
            action=document["decision"],
            feature_status=document["feature_status"],
            replayed=stored.replayed,
            rule_hits=document["rule_hits"],
        )
        if document["feature_status"] == "unavailable":
            resolved_runtime.metrics.velocity_failures.labels("degraded").inc()
        headers = {
            "X-Decision-Replayed": "true" if stored.replayed else "false",
        }
        rate_limit = getattr(request.state, "rate_limit", None)
        if rate_limit is not None:
            headers.update(_rate_limit_headers(rate_limit))
        return Response(
            content=stored.response_json,
            media_type="application/json",
            headers=headers,
        )

    def score(
        tx: Transaction,
        _tenant: TenantContext = Depends(require_decision_write),
    ):
        missing = [feature for feature in FEATURES if feature not in tx.features]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"Missing features: {missing}"
            )
        try:
            legacy = _load_legacy_runtime()
        except LegacyModelUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Legacy scoring model is unavailable; use /v1/decisions "
                    "or install an artifact"
                ),
            ) from exc

        import pandas as pd

        row = pd.DataFrame(
            [[tx.features[feature] for feature in FEATURES]],
            columns=FEATURES,
        )
        probability = float(legacy.model.predict_proba(row)[0, 1])
        ml_decision = legacy_action_for_probability(probability).value
        decision, hits = rules.combine(ml_decision, tx.features, tx.context)
        rule_hits = [
            {"rule": hit.rule, "action": hit.action, "reason": hit.reason}
            for hit in hits
        ]

        reasons = []
        if legacy.explainer is not None:
            shap_values = legacy.explainer.shap_values(row)[0]
            ranked = sorted(
                zip(FEATURES, shap_values),
                key=lambda item: abs(item[1]),
                reverse=True,
            )
            reasons = [
                {
                    "feature": feature,
                    "pushed_toward": "fraud" if value > 0 else "legit",
                    "weight": round(float(value), 3),
                }
                for feature, value in ranked[:4]
            ]
        return {
            "model_version": legacy.model_version,
            "fraud_probability": round(probability, 4),
            "risk_score": round(probability * 100, 1),
            "ml_decision": ml_decision,
            "decision": decision,
            "rule_hits": rule_hits,
            "reasons": reasons,
        }

    if operator_endpoints_enabled:
        application.post(
            "/score",
            deprecated=True,
            summary="Deprecated PCA demo scorer",
            description=(
                "Local/test compatibility endpoint; new integrations must use "
                "POST /v1/decisions."
            ),
        )(score)

    return application


app = create_app()
