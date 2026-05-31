"""multiple_handlers.py — Using separate handlers for different domains.

A single application often caches data with different TTLs and prefixes.
This example shows how to create one CacheHandler per logical domain so
TTL, prefix, and future policy settings are isolated between concerns.

Run (with Redis on localhost:6379):
    python examples/multiple_handlers.py
"""

import json

from cashcov import CacheHandler

REDIS_ADDR = "localhost:6379"


def fetch_session(key: str) -> str:
    print(f"  [session generator] building session {key!r}")
    return json.dumps({"session_id": key, "user": "bob", "role": "admin"})


def fetch_config(key: str) -> str:
    print(f"  [config generator] loading config {key!r}")
    return json.dumps({"feature_flags": {"dark_mode": True, "beta": False}})


def fetch_rate_limit(key: str) -> str:
    print(f"  [rate-limit generator] initialising counter for {key!r}")
    return json.dumps({"requests": 0, "window_start": 0})


def main() -> None:
    # Short-lived session data
    session_cache = CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="sessions",
        ttl=900,  # 15 minutes
    )

    # Longer-lived application config
    config_cache = CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="config",
        ttl=3600,  # 1 hour
    )

    # Very short-lived rate-limit counters
    rate_limit_cache = CacheHandler(
        redis_addr=REDIS_ADDR,
        prefix="ratelimit",
        ttl=60,  # 1 minute
    )

    try:
        print("=== Session cache (TTL 15 min) ===")
        raw = session_cache.get_or_refresh("sess:abc123", fetch_session)
        print(f"  {json.loads(raw)}")

        print("\n=== Config cache (TTL 1 hour) ===")
        raw = config_cache.get_or_refresh("app:v2", fetch_config)
        print(f"  {json.loads(raw)}")

        print("\n=== Rate-limit cache (TTL 1 min) ===")
        raw = rate_limit_cache.get_or_refresh("user:42:/api/search", fetch_rate_limit)
        print(f"  {json.loads(raw)}")

        print("\nSecond round — all should be cache hits:")
        session_cache.get_or_refresh("sess:abc123", fetch_session)
        config_cache.get_or_refresh("app:v2", fetch_config)
        rate_limit_cache.get_or_refresh("user:42:/api/search", fetch_rate_limit)
        print("  (no generator output — all served from cache)")

    finally:
        session_cache.close()
        config_cache.close()
        rate_limit_cache.close()


if __name__ == "__main__":
    main()
