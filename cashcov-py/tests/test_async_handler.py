"""Tests for the async AsyncCacheHandler.

Mirrors test_handler.py — every sync policy and feature has an async
counterpart tested here.
"""

from __future__ import annotations

import asyncio

import fakeredis
import fakeredis.aioredis  # type: ignore[import-untyped]
import pytest

from cashcov import AsyncCacheHandler, CacheMissError
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_counter():
    calls = {"n": 0}

    async def gen() -> str:
        calls["n"] += 1
        return f"value-{calls['n']}"

    return gen, lambda: calls["n"]


async def flush_bg(handler: AsyncCacheHandler) -> None:
    """Allow all pending background tasks to run to completion."""
    if handler._bg_tasks:
        await asyncio.gather(*list(handler._bg_tasks), return_exceptions=True)


# ---------------------------------------------------------------------------
# Basic get / set / delete
# ---------------------------------------------------------------------------


async def test_async_set_and_get(async_cache: AsyncCacheHandler[str]) -> None:
    await async_cache.set("k", "hello")
    result = await async_cache.get("k")
    assert result.value == "hello"
    assert result.from_cache is True


async def test_async_get_missing_raises_key_error(
    async_cache: AsyncCacheHandler[str],
) -> None:
    with pytest.raises(KeyError):
        await async_cache.get("nonexistent")


async def test_async_delete(async_cache: AsyncCacheHandler[str]) -> None:
    await async_cache.set("k", "v")
    await async_cache.delete("k")
    with pytest.raises(KeyError):
        await async_cache.get("k")


async def test_async_prefix_applied(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    async with AsyncCacheHandler[str](async_redis, prefix="ns", ttl=60) as h:
        await h.set("key", "val")
    assert await async_redis.get(b"ns:key") is not None


# ---------------------------------------------------------------------------
# MissFillPolicy.SYNC
# ---------------------------------------------------------------------------


async def test_async_miss_sync_calls_generator(
    async_cache: AsyncCacheHandler[str],
) -> None:
    gen, count = make_counter()
    result = await async_cache.get_or_refresh("k", gen)
    assert result.value == "value-1"
    assert result.from_cache is False
    assert count() == 1


async def test_async_miss_sync_caches_result(
    async_cache: AsyncCacheHandler[str],
) -> None:
    gen, count = make_counter()
    await async_cache.get_or_refresh("k", gen)
    result = await async_cache.get_or_refresh("k", gen)
    assert result.from_cache is True
    assert count() == 1


async def test_async_miss_sync_stampede_protection(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Concurrent misses on the same key should call the generator exactly once."""
    gen, count = make_counter()

    async with AsyncCacheHandler[str](async_redis, prefix="t", ttl=60) as h:
        tasks = [asyncio.create_task(h.get_or_refresh("key", gen)) for _ in range(10)]
        results = await asyncio.gather(*tasks)

    assert count() == 1
    assert all(r.value == "value-1" for r in results)


# ---------------------------------------------------------------------------
# MissFillPolicy.ASYNC
# ---------------------------------------------------------------------------


async def test_async_miss_async_returns_immediately(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis, prefix="t", ttl=60, miss_fill_policy=MissFillPolicy.ASYNC
    ) as h:
        result = await h.get_or_refresh("k", gen)
        assert result.from_cache is False
        assert result.value == "value-1"
        assert count() == 1
        await flush_bg(h)
    assert await async_redis.get(b"t:k") is not None


# ---------------------------------------------------------------------------
# MissFillPolicy.STALE_OR_SYNC
# ---------------------------------------------------------------------------


async def test_async_stale_or_sync_returns_stale_data(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    refresh_done = asyncio.Event()

    async def tracking_gen() -> str:
        val = await gen()
        refresh_done.set()
        return val

    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
        stale_ttl=300,
    ) as h:
        await h.get_or_refresh("k", tracking_gen)
        refresh_done.clear()

        # Delete main key to simulate expiry
        await async_redis.delete(b"t:k")

        result = await h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True
        assert result.value == "value-1"

        # Wait for background refresh
        await asyncio.wait_for(refresh_done.wait(), timeout=2.0)
        await flush_bg(h)

    assert await async_redis.get(b"t:k") is not None


# ---------------------------------------------------------------------------
# MissFillPolicy.FAIL_FAST
# ---------------------------------------------------------------------------


async def test_async_miss_fail_fast_raises(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    async with AsyncCacheHandler[str](
        async_redis, prefix="t", ttl=60, miss_fill_policy=MissFillPolicy.FAIL_FAST
    ) as h:
        with pytest.raises(CacheMissError) as exc_info:
            await h.get_or_refresh("absent")
        assert exc_info.value.key == "absent"


# ---------------------------------------------------------------------------
# MissFillPolicy.COOPERATIVE
# ---------------------------------------------------------------------------


async def test_async_miss_cooperative(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=2.0,
    ) as h:
        tasks = [asyncio.create_task(h.get_or_refresh("k", gen)) for _ in range(5)]
        results = await asyncio.gather(*tasks)
    assert count() == 1
    assert all(r.value == "value-1" for r in results)


# ---------------------------------------------------------------------------
# HitRefreshPolicy.NONE
# ---------------------------------------------------------------------------


async def test_async_hit_refresh_none(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=60,
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as h:
        await h.set("k", "initial")
        await h.get_or_refresh("k", gen)
        await flush_bg(h)
        assert count() == 0


# ---------------------------------------------------------------------------
# HitRefreshPolicy.AHEAD
# ---------------------------------------------------------------------------


async def test_async_hit_refresh_ahead_triggers(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    refresh_done = asyncio.Event()

    async def tracking_gen() -> str:
        val = await gen()
        refresh_done.set()
        return val

    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.5,
    ) as h:
        # Inject key with 10 s remaining (< 50 % of 100 s)
        await async_redis.set(b"t:k", b'"stale"', ex=10)

        await h.get_or_refresh("k", tracking_gen)
        await asyncio.wait_for(refresh_done.wait(), timeout=2.0)
        await flush_bg(h)
        assert count() == 1


async def test_async_hit_refresh_ahead_skips_when_fresh(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.1,
    ) as h:
        await async_redis.set(b"t:k", b'"fresh"', ex=90)
        await h.get_or_refresh("k", gen)
        await flush_bg(h)
        assert count() == 0


# ---------------------------------------------------------------------------
# HitRefreshPolicy.OLDER_THAN
# ---------------------------------------------------------------------------


async def test_async_hit_refresh_older_than(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    refresh_done = asyncio.Event()

    async def tracking_gen() -> str:
        val = await gen()
        refresh_done.set()
        return val

    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
        refresh_older_than=30.0,
    ) as h:
        # age = 100 - 60 = 40 s > 30 s
        await async_redis.set(b"t:k", b'"value"', ex=60)
        await h.get_or_refresh("k", tracking_gen)
        await asyncio.wait_for(refresh_done.wait(), timeout=2.0)
        await flush_bg(h)
        assert count() == 1


# ---------------------------------------------------------------------------
# Refresh cooldown
# ---------------------------------------------------------------------------


async def test_async_refresh_cooldown(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.DEFAULT,
        refresh_cooldown=60.0,
    ) as h:
        await h.set("k", "initial")
        await h.get_or_refresh("k", gen)
        await flush_bg(h)
        first_count = count()

        await h.get_or_refresh("k", gen)
        await flush_bg(h)
        assert count() == first_count


# ---------------------------------------------------------------------------
# ErrorPolicy
# ---------------------------------------------------------------------------


async def test_async_error_surface(async_cache: AsyncCacheHandler[str]) -> None:
    async def bad_gen() -> str:
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await async_cache.get_or_refresh("k", bad_gen)


async def test_async_error_zero_value(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    async def bad_gen() -> str:
        raise ValueError("oops")

    async with AsyncCacheHandler[str](
        async_redis, prefix="t", ttl=60, error_policy=ErrorPolicy.ZERO_VALUE
    ) as h:
        result = await h.get_or_refresh("k", bad_gen)
        assert result.value is None
        assert result.from_cache is False


# ---------------------------------------------------------------------------
# @cached decorator
# ---------------------------------------------------------------------------


async def test_async_cached_decorator(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    gen, count = make_counter()

    # Use NONE to disable background refresh so the generator call count is
    # deterministic (DEFAULT fires a background refresh on every hit).
    async with AsyncCacheHandler[str](
        async_redis, prefix="t", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE
    ) as h:

        @h.cached(key_fn=lambda x: f"item:{x}")
        async def fetch(x: str) -> str:
            return await gen()

        assert await fetch("a") == "value-1"
        assert await fetch("a") == "value-1"  # cache hit — generator not called again
        assert count() == 1

        assert await fetch("b") == "value-2"  # different key → miss
        assert count() == 2


# ---------------------------------------------------------------------------
# aclose / context manager
# ---------------------------------------------------------------------------


async def test_async_aclose_cancels_tasks(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """aclose() should cancel and await pending bg tasks without raising."""
    slow_event = asyncio.Event()

    async with AsyncCacheHandler[str](
        async_redis, prefix="t", ttl=60, hit_refresh_policy=HitRefreshPolicy.DEFAULT
    ) as h:
        await h.set("k", "v")

        async def slow_gen() -> str:
            await asyncio.sleep(10)
            return "slow"

        await h.get_or_refresh("k", slow_gen)
        # aclose() is called by __aexit__ and should not hang


# ---------------------------------------------------------------------------
# HitRefreshPolicy.PROBABILISTIC
# ---------------------------------------------------------------------------


async def test_async_hit_refresh_probabilistic(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """PROBABILISTIC policy triggers background refresh with XFetch.

    Seed _gen_delta with a large value and use a very high beta so the
    condition (delta * beta * -log(rand) > remaining) is essentially certain.
    """
    gen, count = make_counter()
    refresh_done = asyncio.Event()

    async def tracking_gen() -> str:
        val = await gen()
        refresh_done.set()
        return val

    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.PROBABILISTIC,
        probabilistic_beta=1e6,
    ) as h:
        # Seed a large delta → XFetch fires on virtually every call.
        h._gen_delta[h._full_key("k")] = 100.0
        await async_redis.set(b"t:k", b'"initial"', ex=10)

        result = await h.get_or_refresh("k", tracking_gen)
        assert result.from_cache is True
        await asyncio.wait_for(refresh_done.wait(), timeout=2.0)
        await flush_bg(h)
        assert count() == 1


# ---------------------------------------------------------------------------
# disable_hit_refresh per-call override
# ---------------------------------------------------------------------------


async def test_async_disable_hit_refresh_per_call(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """disable_hit_refresh=True suppresses background refresh even under DEFAULT policy."""
    gen, count = make_counter()
    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.DEFAULT,
    ) as h:
        await h.set("k", "initial")
        await h.get_or_refresh("k", gen, disable_hit_refresh=True)
        await flush_bg(h)
        assert count() == 0


# ---------------------------------------------------------------------------
# COOPERATIVE timeout fallback (async)
# ---------------------------------------------------------------------------


async def test_async_cooperative_timeout_fallback(
    async_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """When cooperative_timeout expires, the caller generates directly without caching."""
    gen, count = make_counter()
    lock_held = asyncio.Event()
    release_lock = asyncio.Event()

    async with AsyncCacheHandler[str](
        async_redis,
        prefix="t",
        ttl=60,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=0.05,  # very short timeout
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as h:
        full_key = h._full_key("k")

        async def lock_holder() -> None:
            async with h._lock.acquire(full_key):
                lock_held.set()
                await asyncio.wait_for(release_lock.wait(), timeout=2.0)

        holder = asyncio.create_task(lock_holder())
        await asyncio.wait_for(lock_held.wait(), timeout=1.0)

        # This call should time out and fall back to direct generation.
        result = await h.get_or_refresh("k", gen)

        release_lock.set()
        await holder

    # Timeout fallback: generated but NOT written to Redis.
    assert result.from_cache is False
    assert result.value is not None
    assert count() == 1
    assert await async_redis.get(b"t:k") is None
