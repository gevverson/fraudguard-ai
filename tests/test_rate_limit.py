import base64

import pytest

from pydantic import ValidationError

from src.config import Settings
from src.rate_limit import (
    InMemoryRateLimiter,
    RateLimitUnavailable,
    RedisRateLimiter,
)
from src.velocity import HmacKeyer


def keyer():
    return HmacKeyer(base64.urlsafe_b64decode(b"a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="))


def test_in_memory_bucket_is_tenant_isolated_and_refills():
    now = [10.0]
    limiter = InMemoryRateLimiter(
        keyer(),
        rate_per_second=2,
        burst=2,
        clock=lambda: now[0],
    )
    assert limiter.consume("tenant-a").remaining == 1
    assert limiter.consume("tenant-a").remaining == 0
    rejected = limiter.consume("tenant-a")
    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 1
    assert limiter.consume("tenant-b").allowed is True
    now[0] += 0.5
    assert limiter.consume("tenant-a").allowed is True


class InvalidRedis:
    def evalsha(self, *_args):
        return [1]


def test_redis_limiter_fails_closed_on_invalid_protocol():
    limiter = RedisRateLimiter(
        InvalidRedis(), keyer(), rate_per_second=1, burst=1
    )
    with pytest.raises(RateLimitUnavailable, match="invalid"):
        limiter.consume("tenant-a")


def test_settings_reject_nonpositive_rate_limits():
    with pytest.raises(ValidationError):
        Settings(decision_rate_limit_per_second=0)
    with pytest.raises(ValidationError):
        Settings(decision_rate_limit_burst=0)


def test_limits_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        InMemoryRateLimiter(keyer(), rate_per_second=0, burst=1)
