"""Trusted server-side velocity features for the shadow decision service."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from redis.exceptions import NoScriptError, RedisError

FEATURE_VERSION = "velocity-v1"
WINDOWS_MS = (10_000, 60_000, 300_000)
MAX_WINDOW_MS = WINDOWS_MS[-1]
STATE_TTL_MS = MAX_WINDOW_MS + 60_000


class SubjectKind(StrEnum):
    PAYMENT_METHOD = "payment_method"
    CUSTOMER = "customer"
    ACCOUNT = "account"
    DEVICE = "device"
    BENEFICIARY = "beneficiary"


class VelocityStatus(StrEnum):
    AVAILABLE = "available"
    WARMING = "warming"
    UNAVAILABLE = "unavailable"


class ObservationKind(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    PARTIAL = "partial"


class VelocityError(RuntimeError):
    pass


class VelocityUnavailable(VelocityError):
    pass


class VelocityProtocolError(VelocityUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class VelocityCounts:
    last_10s: int
    last_60s: int
    last_300s: int


@dataclass(frozen=True, slots=True)
class VelocityEvent:
    tenant_id: str
    transaction_id: str
    subjects: Mapping[SubjectKind, str]

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.transaction_id:
            raise ValueError("tenant_id and transaction_id are required")
        normalized: dict[SubjectKind, str] = {}
        for raw_kind, value in self.subjects.items():
            kind = raw_kind if isinstance(raw_kind, SubjectKind) else SubjectKind(raw_kind)
            if not value:
                raise ValueError(f"{kind.value} identifier cannot be empty")
            normalized[kind] = value
        if not normalized:
            raise ValueError("at least one velocity subject is required")
        object.__setattr__(self, "subjects", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class VelocitySnapshot:
    status: VelocityStatus
    observed_at: datetime | None
    counts: Mapping[SubjectKind, VelocityCounts]
    observation: ObservationKind | None
    feature_version: str = FEATURE_VERSION

    @classmethod
    def unavailable(cls) -> "VelocitySnapshot":
        return cls(
            status=VelocityStatus.UNAVAILABLE,
            observed_at=None,
            counts=MappingProxyType({}),
            observation=None,
        )

    @property
    def max_count_60s(self) -> int:
        return max((count.last_60s for count in self.counts.values()), default=0)


@dataclass(frozen=True, slots=True)
class VelocityReadiness:
    ready: bool
    backend: str
    production_safe: bool
    code: str


@runtime_checkable
class VelocityStore(Protocol):
    def observe(self, event: VelocityEvent) -> VelocitySnapshot: ...

    def readiness(self) -> VelocityReadiness: ...


class MillisecondClock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class HmacKeyer:
    secret: bytes = field(repr=False)
    key_version: str = "v1"

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("velocity HMAC secret must contain at least 32 bytes")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", self.key_version) is None:
            raise ValueError("invalid velocity HMAC key version")

    def _digest(self, domain: str, *parts: str) -> str:
        digest = hmac.new(self.secret, digestmod=hashlib.sha256)
        for part in (domain, self.key_version, *parts):
            encoded = part.encode()
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()[:32]

    def tenant_tag(self, tenant_id: str) -> str:
        return f"t.{self._digest('tenant', tenant_id)}"

    def epoch_key(self, tenant_id: str) -> str:
        tag = self.tenant_tag(tenant_id)
        return f"fg:{{{tag}}}:velocity:v1:{self.key_version}:epoch"

    def subject_key(
        self, tenant_id: str, kind: SubjectKind, identifier: str
    ) -> str:
        tag = self.tenant_tag(tenant_id)
        digest = self._digest("subject", tenant_id, kind.value, identifier)
        return (
            f"fg:{{{tag}}}:velocity:v1:{self.key_version}:"
            f"{kind.value}:{digest}"
        )

    def event_member(self, tenant_id: str, transaction_id: str) -> str:
        return self._digest("event", tenant_id, transaction_id)


def _observation_kind(additions: list[bool]) -> ObservationKind:
    if all(additions):
        return ObservationKind.NEW
    if not any(additions):
        return ObservationKind.REPLAY
    return ObservationKind.PARTIAL


def _snapshot(
    *,
    now_ms: int,
    warming: bool,
    ordered_kinds: list[SubjectKind],
    counts: list[VelocityCounts],
    additions: list[bool],
) -> VelocitySnapshot:
    return VelocitySnapshot(
        status=VelocityStatus.WARMING if warming else VelocityStatus.AVAILABLE,
        observed_at=datetime.fromtimestamp(now_ms / 1000, tz=UTC),
        counts=MappingProxyType(dict(zip(ordered_kinds, counts, strict=True))),
        observation=_observation_kind(additions),
    )


class InMemoryVelocityStore:
    """Deterministic single-process store for local development and tests."""

    def __init__(
        self,
        keyer: HmacKeyer,
        *,
        clock: MillisecondClock | None = None,
        warmup_ms: int = MAX_WINDOW_MS,
    ) -> None:
        if warmup_ms < 0:
            raise ValueError("warmup_ms cannot be negative")
        self._keyer = keyer
        self._clock = clock or SystemClock()
        self._warmup_ms = warmup_ms
        self._lock = threading.RLock()
        self._epochs: dict[str, int] = {}
        self._events: dict[str, dict[str, int]] = {}

    def readiness(self) -> VelocityReadiness:
        return VelocityReadiness(
            ready=True,
            backend="memory",
            production_safe=False,
            code="local_only",
        )

    def observe(self, event: VelocityEvent) -> VelocitySnapshot:
        ordered = sorted(event.subjects.items(), key=lambda item: item[0].value)
        with self._lock:
            now_ms = self._clock.now_ms()
            epoch_key = self._keyer.epoch_key(event.tenant_id)
            epoch_ms = self._epochs.setdefault(epoch_key, now_ms)
            cutoff = now_ms - MAX_WINDOW_MS
            member = self._keyer.event_member(event.tenant_id, event.transaction_id)
            additions: list[bool] = []
            result_counts: list[VelocityCounts] = []

            for kind, identifier in ordered:
                key = self._keyer.subject_key(event.tenant_id, kind, identifier)
                bucket = self._events.setdefault(key, {})
                stale = [
                    candidate
                    for candidate, timestamp in bucket.items()
                    if timestamp <= cutoff
                ]
                for stale_member in stale:
                    del bucket[stale_member]

                added = member not in bucket
                if added:
                    bucket[member] = now_ms
                additions.append(added)
                values = tuple(
                    sum(
                        now_ms - window < timestamp <= now_ms
                        for timestamp in bucket.values()
                    )
                    for window in WINDOWS_MS
                )
                result_counts.append(VelocityCounts(*values))

            return _snapshot(
                now_ms=now_ms,
                warming=now_ms - epoch_ms < self._warmup_ms,
                ordered_kinds=[kind for kind, _ in ordered],
                counts=result_counts,
                additions=additions,
            )


OBSERVE_LUA = r"""
if #KEYS < 2 then
    return redis.error_reply("velocity requires epoch and subject keys")
end

local event_member = ARGV[1]
local state_ttl_ms = tonumber(ARGV[2])
local warmup_ms = tonumber(ARGV[3])
local window_count = tonumber(ARGV[4])

if not state_ttl_ms or not warmup_ms or not window_count then
    return redis.error_reply("invalid velocity arguments")
end
if #ARGV ~= 4 + window_count then
    return redis.error_reply("velocity window count mismatch")
end

local windows = {}
local previous = 0
for index = 1, window_count do
    local window = tonumber(ARGV[4 + index])
    if not window or window <= previous then
        return redis.error_reply("velocity windows must be ascending")
    end
    windows[index] = window
    previous = window
end

local max_window_ms = windows[window_count]
if state_ttl_ms <= max_window_ms then
    return redis.error_reply("velocity TTL must exceed maximum window")
end

local server_time = redis.call("TIME")
local now_ms =
    (tonumber(server_time[1]) * 1000) +
    math.floor(tonumber(server_time[2]) / 1000)

local epoch = redis.call("GET", KEYS[1])
if not epoch then
    -- The epoch represents completion of the tenant/key-version warm-up.
    -- It must outlive the rolling subject buckets or the tenant repeatedly
    -- returns to WARMING and silently suppresses velocity rules.
    redis.call("SET", KEYS[1], tostring(now_ms), "NX")
    epoch = redis.call("GET", KEYS[1])
end
local epoch_ms = tonumber(epoch)
if not epoch_ms then
    return redis.error_reply("invalid velocity epoch")
end
local warming = 0
if now_ms - epoch_ms < warmup_ms then
    warming = 1
end

local output = {now_ms, epoch_ms, warming}
local cutoff = now_ms - max_window_ms
for key_index = 2, #KEYS do
    local key = KEYS[key_index]
    redis.call("ZREMRANGEBYSCORE", key, "-inf", tostring(cutoff))
    local added = redis.call("ZADD", key, "NX", now_ms, event_member)
    table.insert(output, added)
    if added == 1 then
        redis.call("PEXPIRE", key, state_ttl_ms)
    end
    for window_index = 1, window_count do
        local lower = "(" .. tostring(now_ms - windows[window_index])
        local count = redis.call("ZCOUNT", key, lower, tostring(now_ms))
        table.insert(output, count)
    end
end
return output
"""

READINESS_LUA = r"""
-- FraudGuard velocity readiness probe: exercise the production write path
-- without tenant or transaction identifiers and expire all probe state.
local server_time = redis.call("TIME")
local now_ms =
    (tonumber(server_time[1]) * 1000) +
    math.floor(tonumber(server_time[2]) / 1000)
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", tostring(now_ms - 5000))
redis.call("ZADD", KEYS[1], "NX", now_ms, ARGV[1])
local count = redis.call("ZCOUNT", KEYS[1], "-inf", "+inf")
redis.call("PEXPIRE", KEYS[1], 5000)
redis.call("SET", KEYS[2], ARGV[1], "PX", 5000)
local stored = redis.call("GET", KEYS[2])
local string_ok = 0
if stored == ARGV[1] then
    string_ok = 1
end
return {now_ms, count, string_ok}
"""


class RedisCommands(Protocol):
    def ping(self) -> Any: ...

    def evalsha(self, sha: str, numkeys: int, *keys_and_args: object) -> Any: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


class RedisVelocityStore:
    """Shared exact sliding-window store using Redis server time and Lua."""

    def __init__(
        self,
        client: RedisCommands,
        keyer: HmacKeyer,
        *,
        warmup_ms: int = MAX_WINDOW_MS,
        state_ttl_ms: int = STATE_TTL_MS,
    ) -> None:
        if warmup_ms < 0:
            raise ValueError("warmup_ms cannot be negative")
        if state_ttl_ms <= MAX_WINDOW_MS:
            raise ValueError("state TTL must exceed maximum window")
        self._client = client
        self._keyer = keyer
        self._warmup_ms = warmup_ms
        self._state_ttl_ms = state_ttl_ms
        self._sha = hashlib.sha1(
            OBSERVE_LUA.encode(), usedforsecurity=False
        ).hexdigest()

    def readiness(self) -> VelocityReadiness:
        try:
            if not self._client.ping():
                raise VelocityProtocolError("Redis PING returned false")
            raw = self._client.eval(
                READINESS_LUA,
                2,
                "fg:{readiness}:velocity:probe:zset",
                "fg:{readiness}:velocity:probe:epoch",
                str(time.time_ns()),
            )
            ready = (
                isinstance(raw, (list, tuple))
                and len(raw) == 3
                and int(raw[0]) > 0
                and int(raw[1]) > 0
                and int(raw[2]) == 1
            )
        except (
            RedisError,
            VelocityProtocolError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            ready = False
        return VelocityReadiness(
            ready=ready,
            backend="redis",
            production_safe=True,
            code="ready" if ready else "redis_probe_failed",
        )

    def _execute(self, keys: list[str], args: list[object]) -> Any:
        payload = [*keys, *args]
        try:
            return self._client.evalsha(self._sha, len(keys), *payload)
        except NoScriptError:
            try:
                return self._client.eval(OBSERVE_LUA, len(keys), *payload)
            except RedisError as exc:
                raise VelocityUnavailable("Redis velocity operation failed") from exc
        except RedisError as exc:
            raise VelocityUnavailable("Redis velocity operation failed") from exc

    def observe(self, event: VelocityEvent) -> VelocitySnapshot:
        ordered = sorted(event.subjects.items(), key=lambda item: item[0].value)
        keys = [self._keyer.epoch_key(event.tenant_id)]
        keys.extend(
            self._keyer.subject_key(event.tenant_id, kind, identifier)
            for kind, identifier in ordered
        )
        args: list[object] = [
            self._keyer.event_member(event.tenant_id, event.transaction_id),
            self._state_ttl_ms,
            self._warmup_ms,
            len(WINDOWS_MS),
            *WINDOWS_MS,
        ]
        raw = self._execute(keys, args)
        expected_length = 3 + len(ordered) * (1 + len(WINDOWS_MS))
        if not isinstance(raw, (list, tuple)) or len(raw) != expected_length:
            raise VelocityProtocolError("invalid Redis velocity response")

        try:
            now_ms = int(raw[0])
            warming = bool(int(raw[2]))
            cursor = 3
            additions: list[bool] = []
            counts: list[VelocityCounts] = []
            for _kind, _identifier in ordered:
                additions.append(bool(int(raw[cursor])))
                cursor += 1
                values = tuple(
                    int(raw[cursor + offset]) for offset in range(len(WINDOWS_MS))
                )
                cursor += len(WINDOWS_MS)
                if any(value < 0 for value in values):
                    raise ValueError("negative count")
                counts.append(VelocityCounts(*values))
        except (TypeError, ValueError, OverflowError) as exc:
            raise VelocityProtocolError("invalid Redis velocity response") from exc

        return _snapshot(
            now_ms=now_ms,
            warming=warming,
            ordered_kinds=[kind for kind, _ in ordered],
            counts=counts,
            additions=additions,
        )
