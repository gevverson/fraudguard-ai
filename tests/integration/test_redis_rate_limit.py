import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis import Redis

from src.rate_limit import RedisRateLimiter
from src.velocity import HmacKeyer


pytestmark = pytest.mark.integration
REDIS_URL = os.getenv("FRAUDGUARD_TEST_REDIS_URL")


@pytest.fixture
def redis_environment():
    if not REDIS_URL:
        pytest.skip("Redis integration URL is not configured")
    client = Redis.from_url(REDIS_URL, decode_responses=False)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def limiter(client, *, burst=20):
    return RedisRateLimiter(
        client,
        HmacKeyer(b"k" * 32),
        rate_per_second=1,
        burst=burst,
    )


def test_real_redis_quota_is_atomic_under_concurrency(redis_environment):
    shared = limiter(redis_environment)
    with ThreadPoolExecutor(max_workers=40) as executor:
        results = list(executor.map(lambda _index: shared.consume("tenant-secret"), range(60)))
    assert sum(result.allowed for result in results) == 20
    assert sum(not result.allowed for result in results) == 40


def test_real_redis_keys_do_not_contain_tenant_identifiers(redis_environment):
    limiter(redis_environment, burst=1).consume("tenant-secret-value")
    keys = [key.decode() for key in redis_environment.scan_iter(match="fg:*")]
    assert keys
    assert "tenant-secret-value" not in " ".join(keys)


def test_real_redis_limiter_recovers_after_script_flush(redis_environment):
    shared = limiter(redis_environment, burst=2)
    assert shared.consume("tenant-a").allowed is True
    redis_environment.script_flush()
    second = shared.consume("tenant-a")
    assert second.allowed is True
    assert second.remaining == 0
