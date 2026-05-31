"""basic.py — Minimal cashcov usage.

How get_or_refresh works
------------------------
1. cashcov checks Redis for the key.
2. **Cache hit**: the stored JSON string is returned immediately.
   The *generator* is NOT called.
3. **Cache miss**: cashcov calls your *generator* function, passing the cache
   key as a plain string.  The generator must return a JSON-encoded string.
   cashcov writes the result to Redis (according to the miss-fill policy) and
   returns it to you.

The generator is a plain Python callable ``(key: str) -> str``.  Under the
hood, cashcov wraps it in a C function pointer (via ctypes) that the Go
library calls back into Python.  You never need to manage this yourself —
just pass the function and cashcov handles the rest.

Run (with Redis on localhost:6379):
    python examples/basic.py
"""

import json
import time

from cashcov import CacheHandler

REDIS_ADDR = "localhost:6379"

call_count = 0


def fetch_user(key: str) -> str:
    """Simulate a slow database lookup.

    This function is the *generator*: cashcov calls it only on a cache miss.
    It receives the cache key as a string and must return a JSON-encoded value.
    """
    global call_count
    call_count += 1
    print(f"  [generator] fetching {key!r} from DB (call #{call_count})")
    time.sleep(0.1)  # simulate latency
    user = {"id": key, "name": "Alice", "email": "alice@example.com"}
    return json.dumps(user)  # must be a JSON string


def main() -> None:
    with CacheHandler(redis_addr=REDIS_ADDR, prefix="example:basic", ttl=60) as cache:
        print("First call — cache miss, generator is invoked:")
        raw = cache.get_or_refresh("user:1", fetch_user)
        print(f"  result: {json.loads(raw)}")

        print("\nSecond call — cache hit, generator is NOT called:")
        raw = cache.get_or_refresh("user:1", fetch_user)
        print(f"  result: {json.loads(raw)}")

        print(f"\nGenerator was called {call_count} time(s) in total.")


if __name__ == "__main__":
    main()
