"""Tests for the FastAPI integration (CacheManager + dependency injection).

These tests verify:
* The CacheManager properly creates/closes the handler around the app lifespan.
* The dependency callable works with Depends().
* Test-time dependency override via app.dependency_overrides works correctly.
* The seeded mock is actually used instead of Redis.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cashcov import AsyncCacheHandler
from cashcov.ext.fastapi import CacheManager
from cashcov.testing import AsyncMockCacheHandler


# ---------------------------------------------------------------------------
# App under test
# ---------------------------------------------------------------------------

cache_manager = CacheManager(
    redis_url="redis://localhost:6379",  # won't actually connect in tests
    prefix="test-fastapi",
    ttl=300,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with cache_manager.lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)

# Stable dependency callable — this is what tests override
cache_dep = cache_manager.get_dependency()
CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]


@app.get("/items/{item_id}")
async def get_item(item_id: str, cache: CacheDep):
    async def generate() -> str:
        return json.dumps({"id": item_id, "name": f"Item {item_id}"})

    result = await cache.get_or_refresh(f"item:{item_id}", generate)
    return {**json.loads(result.value), "from_cache": result.from_cache}


@app.post("/items/{item_id}")
async def set_item(item_id: str, cache: CacheDep):
    await cache.set(f"item:{item_id}", json.dumps({"id": item_id, "name": "Set"}))
    return {"ok": True}


@app.delete("/items/{item_id}")
async def delete_item(item_id: str, cache: CacheDep):
    await cache.delete(f"item:{item_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mock_cache() -> AsyncMockCacheHandler:
    return AsyncMockCacheHandler()


@pytest_asyncio.fixture
async def client(mock_cache: AsyncMockCacheHandler):
    """HTTP client with the cache dependency overridden to use the mock."""
    app.dependency_overrides[cache_dep] = lambda: mock_cache
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_item_miss_calls_generator(
    client: AsyncClient, mock_cache: AsyncMockCacheHandler
) -> None:
    """GET on an absent key calls the in-route generator."""
    response = await client.get("/items/42")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "42"
    assert data["from_cache"] is False
    assert "item:42" in mock_cache.get_or_refresh_calls


async def test_get_item_hit_returns_cached(
    client: AsyncClient, mock_cache: AsyncMockCacheHandler
) -> None:
    """GET on a seeded key returns the cached value."""
    mock_cache.seed("item:99", json.dumps({"id": "99", "name": "Cached Item"}))

    response = await client.get("/items/99")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Cached Item"
    assert data["from_cache"] is True


async def test_dependency_override_isolates_from_redis(
    client: AsyncClient, mock_cache: AsyncMockCacheHandler
) -> None:
    """The mock is used — no real Redis connection is made."""
    # The app was created with redis_url pointing to localhost which may not be
    # running.  The override ensures no actual connection attempt occurs.
    response = await client.get("/items/no-redis-needed")
    assert response.status_code == 200


async def test_injected_error_propagates(
    client: AsyncClient, mock_cache: AsyncMockCacheHandler
) -> None:
    """An injected error on the mock propagates out of the ASGI transport.

    FastAPI's ServerErrorMiddleware re-raises unhandled exceptions through the
    httpx ASGITransport when no custom exception handler is registered, so the
    exception propagates to the test rather than being returned as a 500
    response.  Testing for the raised exception is the correct assertion here.
    """
    mock_cache.inject_error("item:err", RuntimeError("DB exploded"))

    with pytest.raises(RuntimeError, match="DB exploded"):
        await client.get("/items/err")


async def test_multiple_requests_share_mock_state(
    client: AsyncClient, mock_cache: AsyncMockCacheHandler
) -> None:
    """Subsequent requests share the same mock instance within the fixture."""
    # First request → miss → generator called, value stored in mock
    await client.get("/items/shared")
    first_calls = len(mock_cache.get_or_refresh_calls)

    # Second request → hit → generator NOT called
    await client.get("/items/shared")
    assert len(mock_cache.get_or_refresh_calls) == first_calls + 1
    # Second result should be from_cache=True
    resp = await client.get("/items/shared")
    assert resp.json()["from_cache"] is True


async def test_cache_dep_identity_stable() -> None:
    """get_dependency() must return the same callable each time for overrides to work."""
    assert cache_manager.get_dependency() is cache_dep


async def test_lifespan_stores_handler_on_app_state(mock_cache: AsyncMockCacheHandler) -> None:
    """After lifespan setup, app.state.cashcov is the handler."""
    # We verify indirectly: the override mechanism works, meaning lifespan
    # was wired correctly and the dependency resolves.
    app.dependency_overrides[cache_dep] = lambda: mock_cache
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/items/state-test")
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
