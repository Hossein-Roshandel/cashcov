"""Tests for the synchronous CacheHandler."""

from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from cashcov import CacheHandler, CacheMissError
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_counter():
    """Return a (generator_fn, call_count_accessor) pair."""
    calls = {"n": 0}

    def gen() -> str:
        calls["n"] += 1
        return f"value-{calls['n']}"

    return gen, lambda: calls["n"]


def flush_bg(handler: CacheHandler) -> None:
    """Wait for all pending background tasks to finish."""
    handler._executor.shutdown(wait=True, cancel_futures=False)
    import concurrent.futures

    handler._executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=10, thread_name_prefix="cashcov-bg"
    )


# ---------------------------------------------------------------------------
# Basic get / set / delete
# ---------------------------------------------------------------------------


def test_set_and_get(cache: CacheHandler[str]) -> None:
    cache.set("k", "hello")
    result = cache.get("k")
    assert result.value == "hello"
    assert result.from_cache is True


def test_get_missing_raises_key_error(cache: CacheHandler[str]) -> None:
    with pytest.raises(KeyError):
        cache.get("nonexistent")


def test_delete(cache: CacheHandler[str]) -> None:
    cache.set("k", "v")
    cache.delete("k")
    with pytest.raises(KeyError):
        cache.get("k")


def test_prefix_applied(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](sync_redis, prefix="ns", ttl=60) as h:
        h.set("key", "val")
    # Raw Redis key should contain prefix
    assert sync_redis.get(b"ns:key") is not None


def test_no_prefix(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](sync_redis, ttl=60) as h:
        h.set("key", "val")
    assert sync_redis.get(b"key") is not None


# ---------------------------------------------------------------------------
# MissFillPolicy.SYNC
# ---------------------------------------------------------------------------


def test_miss_sync_calls_generator(cache: CacheHandler[str]) -> None:
    gen, count = make_counter()
    result = cache.get_or_refresh("k", gen)
    assert result.value == "value-1"
    assert result.from_cache is False
    assert count() == 1


def test_miss_sync_caches_result(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    # Disable background hit-refresh so call count is deterministic.
    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE
    ) as h:
        h.get_or_refresh("k", gen)
        result = h.get_or_refresh("k", gen)
    assert result.from_cache is True
    assert count() == 1  # generator called only once


def test_miss_sync_stampede_protection(sync_redis: fakeredis.FakeRedis) -> None:
    """Only one generator call even under concurrent misses (shared handler).

    The per-key lock lives on the CacheHandler instance, so all threads must
    share the SAME handler object to benefit from stampede protection.
    """
    gen, count = make_counter()
    start = threading.Barrier(5)

    with CacheHandler[str](
        sync_redis, prefix="test", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE
    ) as h:

        def worker():
            start.wait()
            h.get_or_refresh("key", gen)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert count() == 1


# ---------------------------------------------------------------------------
# MissFillPolicy.ASYNC
# ---------------------------------------------------------------------------


def test_miss_async_returns_immediately(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, miss_fill_policy=MissFillPolicy.ASYNC
    ) as h:
        result = h.get_or_refresh("k", gen)
        assert result.from_cache is False
        assert result.value == "value-1"
        assert count() == 1
        flush_bg(h)
    # Value should now be in Redis
    assert sync_redis.get(b"t:k") is not None


def test_miss_async_dedup_window(sync_redis: fakeredis.FakeRedis) -> None:
    """Second miss within dedup_window retries Redis instead of re-generating."""
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.NONE,
        dedup_window=10.0,
    ) as h:
        h.get_or_refresh("k", gen)
        flush_bg(h)
        # Second call — key is now in Redis, dedup should return it from cache
        result = h.get_or_refresh("k", gen)
        assert result.from_cache is True
        assert count() == 1


# ---------------------------------------------------------------------------
# MissFillPolicy.STALE_OR_SYNC
# ---------------------------------------------------------------------------


def test_stale_or_sync_returns_stale_data(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    refreshed = threading.Event()

    def tracking_gen() -> str:
        val = gen()
        refreshed.set()
        return val

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
        stale_ttl=300,
    ) as h:
        # First call: miss → sync fill → writes main + stale key
        result = h.get_or_refresh("k", tracking_gen)
        assert result.value == "value-1"
        refreshed.clear()

        # Simulate main key expiry
        sync_redis.delete(b"t:k")

        # Second call: stale data available → return immediately + bg refresh
        result = h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True
        assert result.value == "value-1"  # stale value

        # Background refresh runs
        assert refreshed.wait(timeout=2), "Background refresh did not complete"
        flush_bg(h)

    # Main key refreshed
    assert sync_redis.get(b"t:k") is not None


def test_stale_or_sync_falls_back_to_sync_when_no_stale(
    sync_redis: fakeredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
        stale_ttl=300,
    ) as h:
        # No stale key — falls back to SYNC, generates synchronously
        result = h.get_or_refresh("k", gen)
        assert result.from_cache is False
        assert count() == 1


# ---------------------------------------------------------------------------
# MissFillPolicy.FAIL_FAST
# ---------------------------------------------------------------------------


def test_miss_fail_fast_raises(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, miss_fill_policy=MissFillPolicy.FAIL_FAST
    ) as h:
        with pytest.raises(CacheMissError) as exc_info:
            h.get_or_refresh("absent")
        assert exc_info.value.key == "absent"


def test_miss_fail_fast_hits_still_work(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, miss_fill_policy=MissFillPolicy.FAIL_FAST
    ) as h:
        h.set("k", "present")
        result = h.get_or_refresh("k")
        assert result.value == "present"


# ---------------------------------------------------------------------------
# MissFillPolicy.COOPERATIVE
# ---------------------------------------------------------------------------


def test_miss_cooperative_single_caller(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=2.0,
    ) as h:
        result = h.get_or_refresh("k", gen)
        assert result.value == "value-1"
        assert count() == 1


# ---------------------------------------------------------------------------
# HitRefreshPolicy.NONE
# ---------------------------------------------------------------------------


def test_hit_refresh_none(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as h:
        h.set("k", "initial")
        h.get_or_refresh("k", gen)
        flush_bg(h)
        assert count() == 0  # No background refresh


# ---------------------------------------------------------------------------
# HitRefreshPolicy.AHEAD
# ---------------------------------------------------------------------------


def test_hit_refresh_ahead_triggers_when_ttl_low(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    refreshed = threading.Event()

    def tracking_gen() -> str:
        val = gen()
        refreshed.set()
        return val

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.5,  # refresh when < 50 s remaining
    ) as h:
        # Manually inject a key with low remaining TTL (10 s < 50 % of 100 s)
        sync_redis.set(b"t:k", b'"stale-value"', ex=10)

        result = h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True

        assert refreshed.wait(timeout=2), "Background refresh not triggered"
        flush_bg(h)
        assert count() == 1


def test_hit_refresh_ahead_skips_when_ttl_high(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.1,  # only refresh when < 10 s remaining
    ) as h:
        # Key has 90 s remaining — above threshold
        sync_redis.set(b"t:k", b'"fresh-value"', ex=90)

        h.get_or_refresh("k", gen)
        flush_bg(h)
        assert count() == 0  # No refresh needed


# ---------------------------------------------------------------------------
# HitRefreshPolicy.OLDER_THAN
# ---------------------------------------------------------------------------


def test_hit_refresh_older_than_triggers(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    refreshed = threading.Event()

    def tracking_gen() -> str:
        val = gen()
        refreshed.set()
        return val

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
        refresh_older_than=30.0,  # refresh entries older than 30 s
    ) as h:
        # Remaining TTL = 60 s → age = 100 - 60 = 40 s > 30 s → should refresh
        sync_redis.set(b"t:k", b'"value"', ex=60)

        h.get_or_refresh("k", tracking_gen)
        assert refreshed.wait(timeout=2)
        flush_bg(h)
        assert count() == 1


# ---------------------------------------------------------------------------
# Refresh cooldown
# ---------------------------------------------------------------------------


def test_refresh_cooldown_prevents_rapid_refresh(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.DEFAULT,
        refresh_cooldown=60.0,  # 60-second cooldown
    ) as h:
        h.set("k", "initial")

        # First call — refresh allowed
        h.get_or_refresh("k", gen)
        flush_bg(h)
        first_count = count()

        # Second call within cooldown — refresh skipped
        h.get_or_refresh("k", gen)
        flush_bg(h)
        assert count() == first_count  # Generator not called again


# ---------------------------------------------------------------------------
# ErrorPolicy
# ---------------------------------------------------------------------------


def test_error_policy_surface_raises(cache: CacheHandler[str]) -> None:
    def bad_gen() -> str:
        raise ValueError("DB is down")

    with pytest.raises(ValueError, match="DB is down"):
        cache.get_or_refresh("k", bad_gen)


def test_error_policy_zero_value_suppresses(sync_redis: fakeredis.FakeRedis) -> None:
    def bad_gen() -> str:
        raise ValueError("DB is down")

    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, error_policy=ErrorPolicy.ZERO_VALUE
    ) as h:
        result = h.get_or_refresh("k", bad_gen)
        assert result.value is None
        assert result.from_cache is False


def test_cache_miss_error_not_suppressed_by_zero_value(
    sync_redis: fakeredis.FakeRedis,
) -> None:
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.FAIL_FAST,
        error_policy=ErrorPolicy.ZERO_VALUE,
    ) as h:
        # CacheMissError must propagate even under ZERO_VALUE
        with pytest.raises(CacheMissError):
            h.get_or_refresh("absent")


# ---------------------------------------------------------------------------
# @cached decorator
# ---------------------------------------------------------------------------


def test_cached_decorator(sync_redis: fakeredis.FakeRedis) -> None:
    gen, count = make_counter()

    # Use NONE to disable background refresh so the generator call count is
    # deterministic (DEFAULT fires a background refresh on every cache hit).
    with CacheHandler[str](
        sync_redis, prefix="t", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE
    ) as h:

        @h.cached(key_fn=lambda x: f"item:{x}")
        def fetch(x: str) -> str:
            return gen()

        assert fetch("a") == "value-1"
        assert fetch("a") == "value-1"  # cache hit — generator not called again
        assert count() == 1

        assert fetch("b") == "value-2"  # different key → miss
        assert count() == 2


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_close(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](sync_redis, prefix="t", ttl=60) as h:
        h.set("k", "v")
    # After __exit__, executor is shut down — no error expected
    # Simply confirm the set was successful before close
    assert sync_redis.get(b"t:k") is not None


# ---------------------------------------------------------------------------
# Per-call overrides
# ---------------------------------------------------------------------------


def test_per_call_ttl_override(sync_redis: fakeredis.FakeRedis) -> None:
    with CacheHandler[str](sync_redis, prefix="t", ttl=300) as h:
        h.get_or_refresh("k", lambda: "v", ttl=5)
    # Key should have a TTL close to 5, not 300
    ttl = sync_redis.ttl(b"t:k")
    assert 0 < ttl <= 5


def test_per_call_miss_fill_override(sync_redis: fakeredis.FakeRedis) -> None:
    """Handler defaults to SYNC; override to FAIL_FAST per-call."""
    with CacheHandler[str](sync_redis, prefix="t", ttl=60) as h:
        with pytest.raises(CacheMissError):
            h.get_or_refresh(
                "k",
                miss_fill_policy=MissFillPolicy.FAIL_FAST,
            )


# ---------------------------------------------------------------------------
# HitRefreshPolicy.PROBABILISTIC
# ---------------------------------------------------------------------------


def test_hit_refresh_probabilistic(sync_redis: fakeredis.FakeRedis) -> None:
    """PROBABILISTIC policy triggers a background refresh with XFetch algorithm.

    We seed _gen_delta with a large value (simulating a slow generator) and use
    a very high beta so the XFetch condition (delta * beta * -log(rand) > remaining)
    is essentially always true, making the test deterministic.
    """
    gen, count = make_counter()
    refreshed = threading.Event()

    def tracking_gen() -> str:
        val = gen()
        refreshed.set()
        return val

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.PROBABILISTIC,
        probabilistic_beta=1e6,
    ) as h:
        # Simulate a slow generator by seeding the generation-time delta.
        # With delta=100 and beta=1e6: 100 * 1e6 * (-log(rand)) > 10 is ~100% certain.
        h._gen_delta[h._full_key("k")] = 100.0
        # Key with 10 s remaining
        sync_redis.set(b"t:k", b'"initial"', ex=10)

        result = h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True
        assert refreshed.wait(timeout=2), "Probabilistic refresh was not triggered"
        flush_bg(h)
        assert count() == 1


# ---------------------------------------------------------------------------
# MissFillPolicy.COOPERATIVE — concurrent callers and timeout fallback
# ---------------------------------------------------------------------------


def test_miss_cooperative_concurrent_waiters(sync_redis: fakeredis.FakeRedis) -> None:
    """Multiple concurrent callers: first one generates, others wait and get the cache result."""
    gen, count = make_counter()
    start = threading.Barrier(5)

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=5.0,
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as h:

        def worker():
            start.wait()
            h.get_or_refresh("k", gen)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Only one generator call — all other waiters found the value in Redis.
    assert count() == 1


def test_miss_cooperative_timeout_fallback(sync_redis: fakeredis.FakeRedis) -> None:
    """When cooperative_timeout expires, the caller generates directly (no caching)."""
    gen, count = make_counter()
    lock_held = threading.Event()
    release_lock = threading.Event()

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=0.05,  # very short timeout
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as h:
        full_key = h._full_key("k")

        # Hold the per-key lock manually to force the next caller to time out.
        def lock_holder() -> None:
            with h._lock.acquire(full_key):
                lock_held.set()
                release_lock.wait(timeout=2.0)

        holder = threading.Thread(target=lock_holder, daemon=True)
        holder.start()
        lock_held.wait(timeout=1.0)

        # This call should time out waiting for the lock and fall back.
        result = h.get_or_refresh("k", gen)

        release_lock.set()
        holder.join()

    # Timeout fallback: value was generated but NOT written to Redis.
    assert result.from_cache is False
    assert result.value is not None
    assert count() == 1
    assert sync_redis.get(b"t:k") is None


# ---------------------------------------------------------------------------
# disable_hit_refresh per-call override
# ---------------------------------------------------------------------------


def test_disable_hit_refresh_per_call(sync_redis: fakeredis.FakeRedis) -> None:
    """disable_hit_refresh=True suppresses background refresh even when hit_refresh_policy is DEFAULT."""
    gen, count = make_counter()
    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.DEFAULT,  # would normally refresh on every hit
    ) as h:
        h.set("k", "initial")
        # Pass disable_hit_refresh=True — no bg refresh should fire.
        h.get_or_refresh("k", gen, disable_hit_refresh=True)
        flush_bg(h)
        assert count() == 0


# ---------------------------------------------------------------------------
# STALE_OR_SYNC — stale shadow key is renewed after background refresh
# ---------------------------------------------------------------------------


def test_stale_or_sync_renews_stale_key(sync_redis: fakeredis.FakeRedis) -> None:
    """After a STALE_OR_SYNC background refresh, the :stale shadow key is also updated."""
    gen, count = make_counter()
    refreshed = threading.Event()

    def tracking_gen() -> str:
        val = gen()
        refreshed.set()
        return val

    with CacheHandler[str](
        sync_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
        stale_ttl=300,
    ) as h:
        # First call: SYNC fill — writes main key + stale key.
        h.get_or_refresh("k", tracking_gen)
        assert count() == 1
        refreshed.clear()

        # Simulate main key expiry.
        sync_redis.delete(b"t:k")

        # Second call: stale hit — triggers background refresh.
        result = h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True

        assert refreshed.wait(timeout=2), "Background refresh did not complete"
        flush_bg(h)

    # Both the main key and the stale shadow key should now exist.
    assert sync_redis.get(b"t:k") is not None, "Main key not refreshed"
    assert sync_redis.get(b"t:k:stale") is not None, "Stale key not renewed"
    assert sync_redis.ttl(b"t:k:stale") > 60, "Stale key TTL should exceed main TTL"
