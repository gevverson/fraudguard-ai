import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis import Redis

from src.velocity import (
    HmacKeyer,
    RedisVelocityStore,
    STATE_TTL_MS,
    SubjectKind,
    VelocityEvent,
    VelocityStatus,
)

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


def event(transaction_id):
    return VelocityEvent(
        tenant_id="tenant-redis-secret",
        transaction_id=transaction_id,
        subjects={SubjectKind.PAYMENT_METHOD: "payment-token-secret"},
    )


def test_real_redis_is_atomic_deduplicated_and_redacted(redis_environment):
    store = RedisVelocityStore(
        redis_environment,
        HmacKeyer(b"k" * 32),
        warmup_ms=0,
    )
    with ThreadPoolExecutor(max_workers=20) as executor:
        snapshots = list(
            executor.map(
                lambda number: store.observe(event(f"txn-redis-{number}")),
                range(100),
            )
        )
    assert sorted(snapshot.max_count_60s for snapshot in snapshots) == list(
        range(1, 101)
    )
    assert store.observe(event("txn-redis-99")).max_count_60s == 100

    serialized_state = []
    for key in redis_environment.scan_iter(match="fg:*"):
        serialized_state.append(key.decode())
        if redis_environment.type(key) == b"zset":
            serialized_state.extend(
                member.decode()
                for member in redis_environment.zrange(key, 0, -1)
            )
    combined = " ".join(serialized_state)
    assert "tenant-redis-secret" not in combined
    assert "payment-token-secret" not in combined
    assert "txn-redis-" not in combined


def test_real_redis_recovers_after_script_flush(redis_environment):
    store = RedisVelocityStore(
        redis_environment,
        HmacKeyer(b"k" * 32),
        warmup_ms=0,
    )
    store.observe(event("txn-before-flush"))
    redis_environment.script_flush()
    snapshot = store.observe(event("txn-after-flush"))
    assert snapshot.max_count_60s == 2
    assert store.readiness().ready is True


def test_real_redis_epoch_is_persistent_after_state_ttl(redis_environment):
    keyer = HmacKeyer(b"k" * 32)
    store = RedisVelocityStore(redis_environment, keyer, warmup_ms=300_000)

    store.observe(event("txn-epoch-created"))
    epoch_key = keyer.epoch_key("tenant-redis-secret")
    assert redis_environment.pttl(epoch_key) == -1

    seconds, microseconds = redis_environment.time()
    now_ms = seconds * 1000 + microseconds // 1000
    redis_environment.set(epoch_key, now_ms - STATE_TTL_MS - 1)

    snapshot = store.observe(event("txn-after-state-ttl"))
    assert snapshot.status is VelocityStatus.AVAILABLE
    assert redis_environment.pttl(epoch_key) == -1
