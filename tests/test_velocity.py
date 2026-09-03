from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import NoScriptError, RedisError

from src.velocity import (
    HmacKeyer,
    InMemoryVelocityStore,
    ObservationKind,
    RedisVelocityStore,
    SubjectKind,
    VelocityEvent,
    VelocityProtocolError,
    VelocityStatus,
    VelocityUnavailable,
)


class FakeClock:
    def __init__(self, now=0):
        self.value = now

    def now_ms(self):
        return self.value


def event(transaction_id, *, tenant="tenant-a", subject="token-secret"):
    return VelocityEvent(
        tenant_id=tenant,
        transaction_id=transaction_id,
        subjects={SubjectKind.PAYMENT_METHOD: subject},
    )


def test_hmac_keys_hide_raw_identifiers_and_share_tenant_hash_tag():
    keyer = HmacKeyer(b"k" * 32)
    first = keyer.subject_key(
        "tenant-secret", SubjectKind.PAYMENT_METHOD, "token-secret"
    )
    second = keyer.subject_key(
        "tenant-secret", SubjectKind.CUSTOMER, "customer-secret"
    )
    combined = first + second + keyer.event_member("tenant-secret", "txn-secret")
    assert "tenant-secret" not in combined
    assert "token-secret" not in combined
    assert "customer-secret" not in combined
    assert "txn-secret" not in combined
    assert first[first.index("{") : first.index("}") + 1] == second[
        second.index("{") : second.index("}") + 1
    ]


def test_hmac_domains_tenants_and_subject_types_do_not_collide():
    keyer = HmacKeyer(b"k" * 32)
    values = {
        keyer.subject_key("tenant-a", SubjectKind.CUSTOMER, "same"),
        keyer.subject_key("tenant-a", SubjectKind.DEVICE, "same"),
        keyer.subject_key("tenant-b", SubjectKind.CUSTOMER, "same"),
        keyer.event_member("tenant-a", "same"),
    }
    assert len(values) == 4


@pytest.mark.parametrize(
    ("secret", "version"),
    [(b"short", "v1"), (b"k" * 32, "bad version")],
)
def test_keyer_rejects_weak_secret_or_invalid_version(secret, version):
    with pytest.raises(ValueError):
        HmacKeyer(secret, version)


def test_in_memory_window_is_open_left_and_closed_right():
    clock = FakeClock()
    store = InMemoryVelocityStore(
        HmacKeyer(b"k" * 32), clock=clock, warmup_ms=0
    )
    assert store.observe(event("txn-1")).counts[
        SubjectKind.PAYMENT_METHOD
    ].last_10s == 1
    clock.value = 9_999
    assert store.observe(event("txn-2")).counts[
        SubjectKind.PAYMENT_METHOD
    ].last_10s == 2
    clock.value = 10_000
    assert store.observe(event("txn-3")).counts[
        SubjectKind.PAYMENT_METHOD
    ].last_10s == 2


def test_duplicate_does_not_increment_or_refresh_original_timestamp():
    clock = FakeClock()
    store = InMemoryVelocityStore(
        HmacKeyer(b"k" * 32), clock=clock, warmup_ms=0
    )
    first = store.observe(event("txn-1"))
    clock.value = 5_000
    replay = store.observe(event("txn-1"))
    clock.value = 10_000
    expired_from_short_window = store.observe(event("txn-1"))
    assert first.observation is ObservationKind.NEW
    assert replay.observation is ObservationKind.REPLAY
    assert replay.counts[SubjectKind.PAYMENT_METHOD].last_10s == 1
    assert expired_from_short_window.counts[
        SubjectKind.PAYMENT_METHOD
    ].last_10s == 0


def test_warming_becomes_available_at_exact_boundary():
    clock = FakeClock()
    store = InMemoryVelocityStore(
        HmacKeyer(b"k" * 32), clock=clock, warmup_ms=300_000
    )
    assert store.observe(event("txn-1")).status is VelocityStatus.WARMING
    clock.value = 300_000
    assert store.observe(event("txn-2")).status is VelocityStatus.AVAILABLE


def test_tenants_subjects_and_subject_types_are_isolated():
    store = InMemoryVelocityStore(HmacKeyer(b"k" * 32), warmup_ms=0)
    store.observe(event("txn-a"))
    other_tenant = store.observe(event("txn-b", tenant="tenant-b"))
    other_subject = store.observe(event("txn-c", subject="other-token"))
    customer = store.observe(
        VelocityEvent(
            tenant_id="tenant-a",
            transaction_id="txn-d",
            subjects={SubjectKind.CUSTOMER: "token-secret"},
        )
    )
    assert other_tenant.max_count_60s == 1
    assert other_subject.max_count_60s == 1
    assert customer.max_count_60s == 1


def test_in_memory_observation_is_thread_safe():
    store = InMemoryVelocityStore(HmacKeyer(b"k" * 32), warmup_ms=0)
    with ThreadPoolExecutor(max_workers=16) as executor:
        snapshots = list(
            executor.map(
                lambda number: store.observe(event(f"txn-{number}")),
                range(100),
            )
        )
    assert sorted(snapshot.max_count_60s for snapshot in snapshots) == list(
        range(1, 101)
    )
    assert store.readiness().production_safe is False


class FakeRedis:
    def __init__(
        self,
        result=None,
        *,
        error=None,
        no_script=False,
        probe_error=None,
        probe_result=None,
    ):
        self.result = result or [1000, 1000, 0, 1, 1, 1, 1]
        self.error = error
        self.no_script = no_script
        self.probe_error = probe_error
        self.probe_result = probe_result
        self.eval_calls = 0

    def ping(self):
        if self.error:
            raise self.error
        return True

    def evalsha(self, *_args):
        if self.error:
            raise self.error
        if self.no_script:
            raise NoScriptError("missing")
        return self.result

    def eval(self, script, *_args):
        self.eval_calls += 1
        if "readiness probe" in script:
            if self.probe_error:
                raise self.probe_error
            return self.probe_result or [1000, 1, 1]
        if self.error:
            raise self.error
        return self.result


def test_redis_snapshot_parsing_and_noscript_recovery():
    client = FakeRedis(no_script=True)
    store = RedisVelocityStore(client, HmacKeyer(b"k" * 32), warmup_ms=0)
    snapshot = store.observe(event("txn-1"))
    assert snapshot.status is VelocityStatus.AVAILABLE
    assert snapshot.max_count_60s == 1
    assert snapshot.observation is ObservationKind.NEW
    assert client.eval_calls == 1


def test_redis_failure_is_explicit_and_readiness_is_false():
    client = FakeRedis(error=RedisError("offline"))
    store = RedisVelocityStore(client, HmacKeyer(b"k" * 32))
    assert store.readiness().ready is False
    with pytest.raises(VelocityUnavailable):
        store.observe(event("txn-1"))


def test_redis_readiness_fails_when_ping_succeeds_but_writes_fail():
    client = FakeRedis(probe_error=RedisError("NOPERM SET or OOM"))
    assert client.ping() is True
    readiness = RedisVelocityStore(client, HmacKeyer(b"k" * 32)).readiness()
    assert readiness.ready is False
    assert readiness.code == "redis_probe_failed"


def test_redis_readiness_rejects_an_invalid_probe_response():
    client = FakeRedis(probe_result=[1000, 1, 0])
    assert RedisVelocityStore(
        client, HmacKeyer(b"k" * 32)
    ).readiness().ready is False


@pytest.mark.parametrize(
    "result",
    [
        [1, 2],
        [1000, 1000, 0, 1, -1, 1, 1],
        [1000, 1000, 0, 1, "bad", 1, 1],
    ],
)
def test_invalid_redis_response_is_a_protocol_failure(result):
    store = RedisVelocityStore(
        FakeRedis(result=result),
        HmacKeyer(b"k" * 32),
    )
    with pytest.raises(VelocityProtocolError):
        store.observe(event("txn-1"))


def test_velocity_event_requires_tenant_transaction_and_subject():
    with pytest.raises(ValueError):
        VelocityEvent("", "txn", {SubjectKind.CUSTOMER: "customer"})
    with pytest.raises(ValueError):
        VelocityEvent("tenant", "txn", {})
    with pytest.raises(ValueError):
        VelocityEvent("tenant", "txn", {SubjectKind.CUSTOMER: ""})
