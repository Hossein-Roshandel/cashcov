"""error_handling.py — Handling generator failures and cache errors.

Demonstrates the two failure modes you need to handle:

1. CacheError — the underlying Redis operation or generator call failed.
   Catch this to apply a fallback or circuit-breaker logic.

2. Generator returning None — the cashcov C layer treats a None return from
   the generator as a generation failure and raises CacheError.  Raise an
   exception inside the generator for the same effect.

Run (with Redis on localhost:6379):
    python examples/error_handling.py
"""

import json
import random

from cashcov import CacheError, CacheHandler

REDIS_ADDR = "localhost:6379"


# ---------------------------------------------------------------------------
# Simulated generators
# ---------------------------------------------------------------------------


def flaky_generator(key: str) -> str:
    """Fails 50% of the time to simulate an unreliable upstream."""
    if random.random() < 0.5:
        raise RuntimeError(f"upstream unavailable for key {key!r}")
    return json.dumps({"key": key, "value": "fresh data"})


def always_fails(_key: str) -> str:
    raise RuntimeError("service is down")


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------

FALLBACK_VALUE = json.dumps({"key": "unknown", "value": "default (fallback)"})


def get_with_fallback(cache: CacheHandler, key: str) -> dict:
    """Return cached/generated data, or a safe fallback on any error."""
    try:
        raw = cache.get_or_refresh(key, flaky_generator)
        return json.loads(raw)
    except CacheError as exc:
        print(f"  [fallback] CacheError for {key!r}: {exc}")
        return json.loads(FALLBACK_VALUE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    with CacheHandler(redis_addr=REDIS_ADDR, prefix="example:errors", ttl=30) as cache:
        print("=== Flaky generator (retrying until we see both outcomes) ===")
        for attempt in range(1, 6):
            result = get_with_fallback(cache, f"item:{attempt}")
            print(f"  attempt {attempt}: {result}")

        print("\n=== Generator that always fails ===")
        try:
            cache.get_or_refresh("bad-key", always_fails)
        except CacheError as exc:
            print(f"  Caught expected CacheError: {exc}")

        print("\n=== Pre-populating the cache bypasses the generator ===")
        cache.set("preloaded", json.dumps({"source": "manual set"}), ttl=30)
        raw = cache.get_or_refresh("preloaded", always_fails)
        print(f"  Got from cache (generator never called): {json.loads(raw)}")


if __name__ == "__main__":
    main()
