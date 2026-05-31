"""policies.py — Demonstrates all three cashcov policy axes.

cashcov has three independent axes of configurable behaviour:

  MissFillPolicy    — what happens on a cache MISS  (key not in Redis)
  HitRefreshPolicy  — what happens on a cache HIT   (background refresh logic)
  ErrorPolicy       — how generator errors are surfaced to the caller

This example walks through each axis and shows both handler-level defaults
and per-call overrides.

Run (with Redis on localhost:6379):
    python examples/policies.py
"""

import json
import time

from cashcov import CacheError, CacheHandler
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy

REDIS_ADDR = "localhost:6379"
SEP = "-" * 60


# ---------------------------------------------------------------------------
# Shared generator helpers
# ---------------------------------------------------------------------------


def make_generator(label: str):
    """Return a generator that prints when it is called."""

    def generator(key: str) -> str:
        print(f"    [generator:{label}] called for key={key!r}")
        return json.dumps({"key": key, "source": label, "ts": time.time()})

    return generator


def make_flaky_generator(fail_message: str):
    """Return a generator that always raises."""

    def generator(key: str) -> str:
        raise RuntimeError(fail_message)

    return generator


# ---------------------------------------------------------------------------
# Axis 1: MissFillPolicy
# ---------------------------------------------------------------------------


def demo_miss_fill_sync() -> None:
    """SYNC: acquire lock, double-check, generate, write, return.

    Best for: strong consistency, preventing cache stampede.
    Cost:     higher latency on a miss (serialised generation per key).
    """
    print(f"\n{'=' * 60}")
    print("MissFillPolicy.SYNC")
    print("  On a miss: acquire in-process lock → double-check cache →")
    print("  generate → write to Redis → return to caller.")
    print(SEP)

    gen = make_generator("sync")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:sync",
        ttl=30,
        miss_fill_policy=MissFillPolicy.SYNC,
    ) as cache:
        print("  Call 1 — miss, generator runs:")
        print(" ", json.loads(cache.get_or_refresh("item:1", gen)))
        print("  Call 2 — hit, generator silent:")
        print(" ", json.loads(cache.get_or_refresh("item:1", gen)))


def demo_miss_fill_async() -> None:
    """ASYNC: generate and return immediately; write to Redis in background.

    Best for: lowest miss latency when occasional duplicate generation is acceptable.
    Note:     concurrent callers on the *first* miss wave all invoke the generator.
              Use dedup_window to reduce this.
    """
    print(f"\n{'=' * 60}")
    print("MissFillPolicy.ASYNC  (with dedup_window=5s)")
    print("  On a miss: generate → return immediately to caller →")
    print("  write to Redis in background goroutine.")
    print(SEP)

    gen = make_generator("async")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:async",
        ttl=30,
        miss_fill_policy=MissFillPolicy.ASYNC,
        dedup_window=5,  # suppress duplicate generation within 5 s
    ) as cache:
        print("  Call 1 — miss, generator runs, value returned before Redis write:")
        print(" ", json.loads(cache.get_or_refresh("item:2", gen)))
        time.sleep(0.05)  # let the background write complete
        print("  Call 2 — hit (background write landed):")
        print(" ", json.loads(cache.get_or_refresh("item:2", gen)))


def demo_miss_fill_fail_fast() -> None:
    """FAIL_FAST: return CacheError immediately; generator never called.

    Best for: circuit-breaker patterns where a caller manages its own fallback.
    """
    print(f"\n{'=' * 60}")
    print("MissFillPolicy.FAIL_FAST")
    print("  On a miss: raise CacheError immediately (no generator call).")
    print(SEP)

    gen = make_generator("fail_fast")  # will never be called
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:failfast",
        ttl=30,
        miss_fill_policy=MissFillPolicy.FAIL_FAST,
    ) as cache:
        try:
            cache.get_or_refresh("item:3", gen)
        except CacheError as exc:
            print(f"  Got expected CacheError: {exc}")

        # Pre-load the key, then FAIL_FAST succeeds on a hit
        cache.set("item:3", json.dumps({"pre": "loaded"}), ttl=30)
        print("  After cache.set(), FAIL_FAST returns the cached value:")
        print(" ", json.loads(cache.get_or_refresh("item:3", gen)))


def demo_miss_fill_cooperative() -> None:
    """COOPERATIVE: first caller generates; others wait for the lock.

    Best for: shared services where duplicate generation is expensive.
    """
    print(f"\n{'=' * 60}")
    print("MissFillPolicy.COOPERATIVE  (cooperative_timeout=5s)")
    print("  On a miss: first caller acquires lock and generates.")
    print("  Other callers block until the lock is released, then hit cache.")
    print(SEP)

    gen = make_generator("cooperative")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:cooperative",
        ttl=30,
        miss_fill_policy=MissFillPolicy.COOPERATIVE,
        cooperative_timeout=5,
    ) as cache:
        print("  Call 1 — acquires lock, generates:")
        print(" ", json.loads(cache.get_or_refresh("item:4", gen)))
        print("  Call 2 — hit:")
        print(" ", json.loads(cache.get_or_refresh("item:4", gen)))


# ---------------------------------------------------------------------------
# Axis 2: HitRefreshPolicy
# ---------------------------------------------------------------------------


def demo_hit_refresh_ahead() -> None:
    """AHEAD: proactively refresh when remaining TTL < threshold × original TTL.

    Best for: keeping hot keys fresh without ever serving stale data.
    """
    print(f"\n{'=' * 60}")
    print("HitRefreshPolicy.AHEAD  (refresh_ahead_threshold=0.8)")
    print("  On a hit: if remaining TTL < 80% of original TTL,")
    print("  spawn a background refresh.")
    print(SEP)

    gen = make_generator("ahead")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:ahead",
        ttl=30,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.8,  # refresh immediately (threshold is very high)
        generator=gen,  # required: background goroutine uses this
    ) as cache:
        # Prime the cache
        cache.set("item:5", json.dumps({"v": 1}), ttl=30)
        print("  Hit with threshold=0.8 — background refresh will trigger:")
        print(" ", json.loads(cache.get_or_refresh("item:5", gen)))
        time.sleep(0.05)


def demo_hit_refresh_older_than() -> None:
    """OLDER_THAN: refresh when the entry has been in cache longer than N seconds.

    Best for: data that should be refreshed on a wall-clock schedule rather
    than a TTL-fraction basis.
    """
    print(f"\n{'=' * 60}")
    print("HitRefreshPolicy.OLDER_THAN  (refresh_older_than=1s)")
    print("  On a hit: if entry age > 1s, spawn background refresh.")
    print(SEP)

    gen = make_generator("older_than")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:older",
        ttl=30,
        hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
        refresh_older_than=1,  # refresh entries older than 1 second
        generator=gen,  # required: background goroutine uses this
    ) as cache:
        cache.set("item:6", json.dumps({"v": 1}), ttl=30)
        print("  Hit immediately after set — too new to refresh:")
        cache.get_or_refresh("item:6", gen)
        time.sleep(1.1)
        print("  Hit after 1.1s — entry is old enough, background refresh fires:")
        cache.get_or_refresh("item:6", gen)
        time.sleep(0.05)


def demo_hit_refresh_none() -> None:
    """NONE: disable all background refresh. Cache is never updated on hits.

    Best for: read-heavy workloads where stale data is acceptable until TTL expiry.
    """
    print(f"\n{'=' * 60}")
    print("HitRefreshPolicy.NONE")
    print("  On a hit: never trigger background refresh.")
    print(SEP)

    gen = make_generator("none")  # will only run on a miss
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:none",
        ttl=30,
        hit_refresh_policy=HitRefreshPolicy.NONE,
    ) as cache:
        print("  Call 1 — miss, generator runs:")
        cache.get_or_refresh("item:7", gen)
        print("  Call 2 — hit, no background refresh (generator silent):")
        cache.get_or_refresh("item:7", gen)


# ---------------------------------------------------------------------------
# Axis 3: ErrorPolicy
# ---------------------------------------------------------------------------


def demo_error_policy_surface() -> None:
    """SURFACE (default): generator errors propagate to the caller as CacheError."""
    print(f"\n{'=' * 60}")
    print("ErrorPolicy.SURFACE  (default)")
    print("  Generator errors are raised as CacheError.")
    print(SEP)

    gen = make_flaky_generator("upstream is down")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:surface",
        ttl=30,
        error_policy=ErrorPolicy.SURFACE,
    ) as cache:
        try:
            cache.get_or_refresh("item:8", gen)
        except CacheError as exc:
            print(f"  Caught CacheError as expected: {exc}")


def demo_error_policy_zero_value() -> None:
    """ZERO_VALUE: generator errors are suppressed; caller receives None.

    The generator error is swallowed. Useful for non-critical data where
    partial availability is preferable to an error.
    Note: FAIL_FAST's CacheError is never suppressed.
    """
    print(f"\n{'=' * 60}")
    print("ErrorPolicy.ZERO_VALUE")
    print("  Generator errors are suppressed; get_or_refresh returns None.")
    print(SEP)

    gen = make_flaky_generator("upstream is down")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:zeroval",
        ttl=30,
        error_policy=ErrorPolicy.ZERO_VALUE,
    ) as cache:
        result = cache.get_or_refresh("item:9", gen)
        print(f"  Result with suppressed error: {result!r}  (None = zero value)")


# ---------------------------------------------------------------------------
# Per-call policy override
# ---------------------------------------------------------------------------


def demo_per_call_override() -> None:
    """Handler default is SYNC; one call overrides to FAIL_FAST."""
    print(f"\n{'=' * 60}")
    print("Per-call policy override")
    print("  Handler default: MissFillPolicy.SYNC")
    print("  One call overrides to MissFillPolicy.FAIL_FAST")
    print(SEP)

    gen = make_generator("override")
    with CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="demo:override",
        ttl=30,
        miss_fill_policy=MissFillPolicy.SYNC,
    ) as cache:
        print("  Normal call (SYNC) — generator runs on miss:")
        print(" ", json.loads(cache.get_or_refresh("item:10", gen)))

        print("  Per-call FAIL_FAST on a different key — CacheError raised:")
        try:
            cache.get_or_refresh(
                "item:11",
                gen,
                miss_fill_policy=MissFillPolicy.FAIL_FAST,
            )
        except CacheError as exc:
            print(f"  {exc}")

        print("  Back to default SYNC on original key — cache hit:")
        print(" ", json.loads(cache.get_or_refresh("item:10", gen)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("cashcov Python policy demonstrations")
    print("Requires Redis running on localhost:6379")

    # Axis 1: MissFillPolicy
    demo_miss_fill_sync()
    demo_miss_fill_async()
    demo_miss_fill_fail_fast()
    demo_miss_fill_cooperative()

    # Axis 2: HitRefreshPolicy
    demo_hit_refresh_ahead()
    demo_hit_refresh_older_than()
    demo_hit_refresh_none()

    # Axis 3: ErrorPolicy
    demo_error_policy_surface()
    demo_error_policy_zero_value()

    # Per-call override
    demo_per_call_override()

    print(f"\n{'=' * 60}")
    print("All demos complete.")


if __name__ == "__main__":
    main()
