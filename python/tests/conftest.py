"""pytest configuration for cashcov Python tests.

Architecture
------------
Tests are split into two tiers:

  test_policies.py  – pure Python enum-value assertions; no shim, no Redis.
  test_client.py    – end-to-end through the real Go shim into a real Redis.

For the end-to-end tier the shim (libcashcov.so / .dylib) must be compiled
and a Redis server must be reachable.  The fixtures here handle both
requirements gracefully:

  * Shim  – detected at import time; all client tests are skipped when absent.
  * Redis – testcontainers-python is used when Docker is available; falls back
            to the address in CASHCOV_TEST_REDIS_ADDR (default localhost:6379).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shim detection
# Checked once at collection time so individual tests can mark themselves
# without trying (and failing) to import cashcov.
# ---------------------------------------------------------------------------


def _shim_exists() -> bool:
    env = os.environ.get("CASHCOV_LIB_PATH")
    if env:
        return Path(env).exists()
    name = {
        "linux": "libcashcov.so",
        "darwin": "libcashcov.dylib",
        "win32": "cashcov.dll",
    }.get(sys.platform, "libcashcov.so")
    return (Path(__file__).parent.parent / "cashcov" / name).exists()


SHIM_AVAILABLE = _shim_exists()

#: Apply this mark to any test that requires the compiled Go shim.
requires_shim = pytest.mark.skipif(
    not SHIM_AVAILABLE,
    reason="libcashcov not compiled — run `make build-shim` or set CASHCOV_LIB_PATH",
)

# ---------------------------------------------------------------------------
# Redis — testcontainers with env-var / localhost fallback
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_addr() -> str:  # type: ignore[return]
    """Yield a ``host:port`` address pointing at a throwaway Redis instance.

    Priority:
    1. testcontainers RedisContainer (requires Docker)
    2. CASHCOV_TEST_REDIS_ADDR environment variable
    3. localhost:6379 (devcontainer default)
    """
    env_addr = os.environ.get("CASHCOV_TEST_REDIS_ADDR")

    try:
        from testcontainers.redis import RedisContainer  # type: ignore[import]

        with RedisContainer("redis:7-alpine") as container:
            yield f"localhost:{container.get_exposed_port(6379)}"
            return
    except Exception:
        pass  # testcontainers not installed or Docker unavailable

    yield env_addr or "localhost:6379"


@pytest.fixture()
def redis_client(redis_addr: str):
    """A redis-py client connected to the test Redis; skips if unreachable."""
    import redis as redis_lib  # type: ignore[import]

    host, _, port_str = redis_addr.rpartition(":")
    port = int(port_str) if port_str.isdigit() else 6379
    client = redis_lib.Redis(host=host or "localhost", port=port, db=0, decode_responses=True)
    try:
        client.ping()
    except Exception:
        pytest.skip(f"Redis not available at {redis_addr}")

    yield client
    client.close()


@pytest.fixture()
def flush_redis(redis_client):
    """Flush the Redis DB before the test so each test starts with a clean slate."""
    redis_client.flushdb()
    yield


# ---------------------------------------------------------------------------
# CacheHandler factory
# Depends on flush_redis so the DB is always clean before a handler is created.
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_handler(redis_addr: str, flush_redis):  # noqa: ARG001
    """Yield a factory that creates CacheHandler instances connected to the
    test Redis.  All created handlers are closed after the test.

    Usage::

        def test_something(make_handler):
            h = make_handler(ttl=10, miss_fill_policy=MissFillPolicy.ASYNC)
            ...
    """
    if not SHIM_AVAILABLE:
        pytest.skip("libcashcov not compiled — run `make build-shim`")

    from cashcov import CacheHandler

    created: list[CacheHandler] = []

    def _factory(**kwargs) -> CacheHandler:
        kwargs.setdefault("redis_addr", redis_addr)
        kwargs.setdefault("prefix", "pytest")
        kwargs.setdefault("ttl", 30)
        h = CacheHandler(**kwargs)
        created.append(h)
        return h

    yield _factory

    for h in created:
        h.close()


@pytest.fixture()
def handler(make_handler):
    """A single default CacheHandler for tests that need nothing special.

    Background hit-refresh is disabled (HitRefreshNone) so that tests which
    assert on generator call counts remain deterministic — no goroutine races.
    Tests that specifically exercise hit-refresh behaviour use make_handler
    directly with an explicit hit_refresh_policy.
    """
    from cashcov.policies import HitRefreshPolicy

    return make_handler(hit_refresh_policy=HitRefreshPolicy.NONE)
