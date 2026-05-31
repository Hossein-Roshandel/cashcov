"""Shared test fixtures for the cashcov test suite.

Uses ``fakeredis`` for an in-process Redis simulation — no Docker required.
All fixtures are session-scoped by default where safe to do so; test-level
scoped where isolation is required.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis
import fakeredis.aioredis  # type: ignore[import-untyped]

from cashcov import AsyncCacheHandler, CacheHandler
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy


# ---------------------------------------------------------------------------
# Redis fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_redis() -> fakeredis.FakeRedis:
    """Fresh in-memory FakeRedis for each test."""
    client = fakeredis.FakeRedis(decode_responses=False)
    yield client
    client.flushall()
    client.close()


@pytest_asyncio.fixture
async def async_redis() -> fakeredis.aioredis.FakeRedis:
    """Fresh in-memory async FakeRedis for each test."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.flushall()
    await client.aclose()


# ---------------------------------------------------------------------------
# Handler fixtures (default config — override in individual tests as needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(sync_redis: fakeredis.FakeRedis) -> CacheHandler[str]:
    with CacheHandler[str](sync_redis, prefix="test", ttl=60) as h:
        yield h


@pytest_asyncio.fixture
async def async_cache(async_redis: fakeredis.aioredis.FakeRedis) -> AsyncCacheHandler[str]:
    async with AsyncCacheHandler[str](async_redis, prefix="test", ttl=60) as h:
        yield h
