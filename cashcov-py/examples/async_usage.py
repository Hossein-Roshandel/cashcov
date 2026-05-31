"""Async usage example with AsyncCacheHandler.

Run with:
    cd cashcov-py
    uv run python examples/async_usage.py
"""

from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis  # type: ignore[import-untyped]

from cashcov import AsyncCacheHandler
from cashcov.policies import HitRefreshPolicy, MissFillPolicy


async def main() -> None:
    rdb = fakeredis.aioredis.FakeRedis(decode_responses=False)

    call_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Example 1: Basic miss/hit cycle
    # ------------------------------------------------------------------

    print("=== Example 1: Basic async miss/hit ===")

    async def fetch_product(product_id: str) -> str:
        call_counts[product_id] = call_counts.get(product_id, 0) + 1
        await asyncio.sleep(0)  # simulate I/O
        return json.dumps({"id": product_id, "name": f"Product {product_id}"})

    async with AsyncCacheHandler[str](rdb, prefix="store", ttl=300) as cache:
        r1 = await cache.get_or_refresh("p1", lambda: fetch_product("p1"))
        r2 = await cache.get_or_refresh("p1", lambda: fetch_product("p1"))

        print(f"  r1.from_cache={r1.from_cache}  r2.from_cache={r2.from_cache}")
        print(f"  Generator called {call_counts.get('p1', 0)} time(s) (expected: 1)")
    print()

    # ------------------------------------------------------------------
    # Example 2: Concurrent misses — stampede protection
    # ------------------------------------------------------------------

    print("=== Example 2: Concurrent stampede protection ===")

    await rdb.flushall()
    call_counts.clear()

    async def slow_fetch(key: str) -> str:
        call_counts[key] = call_counts.get(key, 0) + 1
        await asyncio.sleep(0.05)  # simulate slow DB query
        return json.dumps({"id": key, "computed": True})

    async with AsyncCacheHandler[str](
        rdb, prefix="store", ttl=300, miss_fill_policy=MissFillPolicy.SYNC
    ) as cache:
        tasks = [
            asyncio.create_task(cache.get_or_refresh("shared", lambda: slow_fetch("shared")))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

    generator_calls = call_counts.get("shared", 0)
    print(f"  10 concurrent requests → generator called {generator_calls} time(s) (expected: 1)")
    assert generator_calls == 1
    print()

    # ------------------------------------------------------------------
    # Example 3: ASYNC fill — return immediately, write in background
    # ------------------------------------------------------------------

    print("=== Example 3: ASYNC fill ===")

    await rdb.flushall()
    call_counts.clear()
    bg_done = asyncio.Event()

    async def tracked_fetch(key: str) -> str:
        call_counts[key] = call_counts.get(key, 0) + 1
        bg_done.set()
        return json.dumps({"id": key})

    async with AsyncCacheHandler[str](
        rdb,
        prefix="store",
        ttl=300,
        miss_fill_policy=MissFillPolicy.ASYNC,
    ) as cache:
        r = await cache.get_or_refresh("async-key", lambda: tracked_fetch("async-key"))
        print(f"  Returned immediately: from_cache={r.from_cache}")
        await asyncio.wait_for(bg_done.wait(), timeout=1.0)
        # Yield to the event loop so the pending _bg_write task can execute.
        await asyncio.sleep(0)
        print("  Background write completed")

        # Next call hits the cache
        r2 = await cache.get_or_refresh("async-key", lambda: tracked_fetch("async-key"))
        print(f"  Second call: from_cache={r2.from_cache}")
    print()

    # ------------------------------------------------------------------
    # Example 4: Refresh-ahead
    # ------------------------------------------------------------------

    print("=== Example 4: Refresh-ahead ===")

    await rdb.flushall()
    call_counts.clear()
    refresh_event = asyncio.Event()

    async def gen_report() -> str:
        call_counts["report"] = call_counts.get("report", 0) + 1
        refresh_event.set()
        return json.dumps({"n": call_counts["report"]})

    async with AsyncCacheHandler[str](
        rdb,
        prefix="store",
        ttl=100,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.5,
    ) as cache:
        # Inject a key with 10 s remaining (< 50 % of 100 s)
        await rdb.set(b"store:report", b'"seed"', ex=10)
        refresh_event.clear()

        result = await cache.get_or_refresh("report", gen_report)
        print(f"  Served from cache: {result.from_cache}")
        await asyncio.wait_for(refresh_event.wait(), timeout=2.0)
        print(f"  Background refresh triggered: {call_counts['report']} time(s)")
    print()

    # ------------------------------------------------------------------
    # Example 5: @cached decorator
    # ------------------------------------------------------------------

    print("=== Example 5: @cached decorator ===")

    await rdb.flushall()
    call_counts.clear()

    # Use NONE so background refresh doesn't add extra generator calls.
    async with AsyncCacheHandler[str](
        rdb, prefix="store", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE
    ) as cache:

        @cache.cached(key_fn=lambda uid: f"user:{uid}")
        async def get_user(uid: str) -> str:
            call_counts[uid] = call_counts.get(uid, 0) + 1
            return json.dumps({"id": uid})

        await get_user("alice")
        await get_user("alice")  # hit — generator not called again
        await get_user("bob")    # different key

        print(f"  alice calls: {call_counts.get('alice', 0)} (expected: 1)")
        print(f"  bob calls:   {call_counts.get('bob', 0)} (expected: 1)")
    print()

    await rdb.aclose()
    print("All async examples completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
