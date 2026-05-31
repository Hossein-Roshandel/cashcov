"""Basic synchronous usage example.

Run with:
    cd cashcov-py
    uv run python examples/basic.py
"""

from __future__ import annotations

import json

import fakeredis

from cashcov import CacheHandler
from cashcov.policies import HitRefreshPolicy, MissFillPolicy

# ---------------------------------------------------------------------------
# Setup (using fakeredis so this example runs without a real Redis server)
# ---------------------------------------------------------------------------

rdb = fakeredis.FakeRedis(decode_responses=False)


# ---------------------------------------------------------------------------
# Example 1: Basic miss-then-hit cycle
# ---------------------------------------------------------------------------

print("=== Example 1: Basic miss/hit ===\n"
      "  (using HitRefreshPolicy.NONE so background refresh does not affect count)")

call_count = 0


def fetch_user(user_id: str) -> str:
    global call_count
    call_count += 1
    print(f"  [DB] Fetching user {user_id!r}...")
    return json.dumps({"id": user_id, "name": "Alice", "role": "admin"})


with CacheHandler[str](
    rdb, prefix="myapp", ttl=300, hit_refresh_policy=HitRefreshPolicy.NONE
) as cache:
    # First call: miss → generator invoked
    result = cache.get_or_refresh("user:1", lambda: fetch_user("1"))
    print(f"  from_cache={result.from_cache}  value={result.value}")

    # Second call: hit → generator NOT invoked
    result = cache.get_or_refresh("user:1", lambda: fetch_user("1"))
    print(f"  from_cache={result.from_cache}  value={result.value}")

print(f"  Generator called {call_count} time(s) (expected: 1)\n")


# ---------------------------------------------------------------------------
# Example 2: ASYNC miss-fill — generate and return immediately
# ---------------------------------------------------------------------------

print("=== Example 2: ASYNC miss-fill ===")

rdb.flushall()
call_count = 0

with CacheHandler[str](
    rdb,
    prefix="myapp",
    ttl=300,
    miss_fill_policy=MissFillPolicy.ASYNC,
) as cache:
    result = cache.get_or_refresh("user:2", lambda: fetch_user("2"))
    print(f"  Returned immediately: from_cache={result.from_cache}")
    # Background thread writes to Redis asynchronously
    import time; time.sleep(0.1)  # let bg thread finish
    result2 = cache.get_or_refresh("user:2", lambda: fetch_user("2"))
    print(f"  Second call: from_cache={result2.from_cache}")
print()


# ---------------------------------------------------------------------------
# Example 3: FAIL_FAST for circuit-breaker patterns
# ---------------------------------------------------------------------------

print("=== Example 3: FAIL_FAST ===")

from cashcov import CacheMissError

rdb.flushall()

with CacheHandler[str](
    rdb,
    prefix="myapp",
    ttl=300,
    miss_fill_policy=MissFillPolicy.FAIL_FAST,
) as cache:
    cache.set("product:1", json.dumps({"id": "1", "name": "Widget"}))

    # Hit — works fine
    result = cache.get_or_refresh("product:1")
    print(f"  Hit: {json.loads(result.value)['name']}")

    # Miss — raises CacheMissError
    try:
        cache.get_or_refresh("product:999")
    except CacheMissError as e:
        print(f"  Miss raises CacheMissError: {e}")
print()


# ---------------------------------------------------------------------------
# Example 4: @cached decorator
# ---------------------------------------------------------------------------

print("=== Example 4: @cached decorator ===\n"
      "  (using HitRefreshPolicy.NONE so call count is deterministic)")

rdb.flushall()
call_count = 0

with CacheHandler[str](
    rdb, prefix="myapp", ttl=300, hit_refresh_policy=HitRefreshPolicy.NONE
) as cache:

    @cache.cached(key_fn=lambda uid: f"user:{uid}")
    def get_user(uid: str) -> str:
        global call_count
        call_count += 1
        return json.dumps({"id": uid, "name": f"User {uid}"})

    val1 = get_user("alice")
    val2 = get_user("alice")  # cache hit
    val3 = get_user("bob")    # different key → miss

    print(f"  alice (call 1): {json.loads(val1)['name']}")
    print(f"  alice (call 2): {json.loads(val2)['name']} (from cache)")
    print(f"  bob (call 1):   {json.loads(val3)['name']}")
    print(f"  Generator called {call_count} time(s) (expected: 2)")
print()


# ---------------------------------------------------------------------------
# Example 5: Refresh-ahead — keep cache warm as TTL drains
# ---------------------------------------------------------------------------

print("=== Example 5: HitRefreshPolicy.AHEAD ===")

import time

rdb.flushall()
refresh_count = 0


def fetch_report() -> str:
    global refresh_count
    refresh_count += 1
    return json.dumps({"generated_at": time.time(), "version": refresh_count})


with CacheHandler[str](
    rdb,
    prefix="myapp",
    ttl=100,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.5,  # refresh when < 50 % TTL remains
) as cache:
    # Manually set a key with only 10 s remaining (< 50 % of 100 s)
    rdb.set(b"myapp:report", fetch_report().encode(), ex=10)
    refresh_count = 0  # reset after seeding

    result = cache.get_or_refresh("report", fetch_report)
    time.sleep(0.1)  # let background refresh finish
    print(f"  Served from cache: {result.from_cache}")
    print(f"  Background refreshes triggered: {refresh_count}")
print()

print("All examples completed successfully.")
