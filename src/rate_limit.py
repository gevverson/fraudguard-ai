"""Tenant-scoped request quotas with atomic Redis enforcement."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from redis.exceptions import NoScriptError, RedisError

from .velocity import HmacKeyer


class RateLimitUnavailable(RuntimeError):
    """Raised when the shared quota backend cannot make a safe decision."""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


@runtime_checkable
class RateLimiter(Protocol):
    def consume(self, tenant_id: str) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """Process-local token bucket for local/test environments only."""

    def __init__(
        self,
        keyer: HmacKeyer,
        *,
        rate_per_second: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_limits(rate_per_second, burst)
        self._keyer = keyer
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._states: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def consume(self, tenant_id: str) -> RateLimitResult:
        key = self._keyer.tenant_tag(tenant_id)
        now = self._clock()
        with self._lock:
            tokens, last = self._states.get(key, (float(self._burst), now))
            tokens = min(
                float(self._burst),
                tokens + max(0.0, now - last) * self._rate,
            )
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._states[key] = (tokens, now)
        return _result(allowed, self._burst, tokens, self._rate)


RATE_LIMIT_LUA = r"""
local now = redis.call("TIME")
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local state = redis.call("HMGET", KEYS[1], "tokens", "last_ms")
local tokens = tonumber(state[1]) or burst
local last_ms = tonumber(state[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - last_ms)
tokens = math.min(burst, tokens + ((elapsed_ms / 1000) * rate))
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end
redis.call("HSET", KEYS[1], "tokens", tostring(tokens), "last_ms", tostring(now_ms))
redis.call("PEXPIRE", KEYS[1], math.max(1000, math.ceil((burst / rate) * 2000)))
local retry_ms = 0
if allowed == 0 then
    retry_ms = math.ceil(((1 - tokens) / rate) * 1000)
end
return {allowed, math.floor(tokens), retry_ms}
"""


class RedisRateLimiter:
    """Shared token bucket using Redis server time and an atomic Lua script."""

    def __init__(
        self,
        client: Any,
        keyer: HmacKeyer,
        *,
        rate_per_second: int,
        burst: int,
    ) -> None:
        _validate_limits(rate_per_second, burst)
        self._client = client
        self._keyer = keyer
        self._rate = rate_per_second
        self._burst = burst
        self._sha = hashlib.sha1(
            RATE_LIMIT_LUA.encode(), usedforsecurity=False
        ).hexdigest()

    def _key(self, tenant_id: str) -> str:
        tag = self._keyer.tenant_tag(tenant_id)
        return f"fg:{{{tag}}}:rate:v1:decisions"

    def consume(self, tenant_id: str) -> RateLimitResult:
        key = self._key(tenant_id)
        args = (self._rate, self._burst)
        try:
            raw = self._client.evalsha(self._sha, 1, key, *args)
        except NoScriptError:
            try:
                raw = self._client.eval(RATE_LIMIT_LUA, 1, key, *args)
            except RedisError as exc:
                raise RateLimitUnavailable("Redis rate limit failed") from exc
        except RedisError as exc:
            raise RateLimitUnavailable("Redis rate limit failed") from exc
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise RateLimitUnavailable("invalid Redis rate limit response")
        try:
            allowed = bool(int(raw[0]))
            remaining = max(0, int(raw[1]))
            retry_ms = max(0, int(raw[2]))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RateLimitUnavailable("invalid Redis rate limit response") from exc
        return RateLimitResult(
            allowed=allowed,
            limit=self._burst,
            remaining=remaining,
            retry_after_seconds=max(1, math.ceil(retry_ms / 1000)) if not allowed else 0,
        )


def _validate_limits(rate_per_second: int, burst: int) -> None:
    if rate_per_second <= 0 or burst <= 0:
        raise ValueError("rate and burst must be positive")


def _result(
    allowed: bool,
    burst: int,
    tokens: float,
    rate_per_second: int,
) -> RateLimitResult:
    retry = 0 if allowed else max(1, math.ceil((1.0 - tokens) / rate_per_second))
    return RateLimitResult(
        allowed=allowed,
        limit=burst,
        remaining=max(0, math.floor(tokens)),
        retry_after_seconds=retry,
    )
