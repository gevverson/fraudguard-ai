"""Low-cardinality operational metrics for the HTTP decision service."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "fraudguard_http_requests_total",
            "HTTP requests by bounded route, method, and status class.",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.request_latency = Histogram(
            "fraudguard_http_request_duration_seconds",
            "HTTP request duration by bounded route.",
            ("route",),
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.decisions = Counter(
            "fraudguard_decisions_total",
            "Durable decisions by action, feature state, and replay status.",
            ("action", "feature_status", "replayed"),
            registry=self.registry,
        )
        self.rule_hits = Counter(
            "fraudguard_rule_hits_total",
            "Rule hits by bounded rule and action.",
            ("rule", "action"),
            registry=self.registry,
        )
        self.velocity_failures = Counter(
            "fraudguard_velocity_failures_total",
            "Trusted velocity failures by configured outcome.",
            ("outcome",),
            registry=self.registry,
        )

    def observe_request(
        self, *, route: str, method: str, status_code: int, duration: float
    ) -> None:
        self.requests.labels(route, method, f"{status_code // 100}xx").inc()
        self.request_latency.labels(route).observe(duration)

    def observe_decision(
        self,
        *,
        action: str,
        feature_status: str,
        replayed: bool,
        rule_hits: list[dict[str, str]],
    ) -> None:
        self.decisions.labels(
            action,
            feature_status,
            "true" if replayed else "false",
        ).inc()
        if not replayed:
            for hit in rule_hits:
                self.rule_hits.labels(hit["rule"], hit["action"]).inc()

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
