"""FastAPI integration for cashcov.

This module provides:

* :class:`CacheManager` — lifecycle manager that creates/closes an
  :class:`~cashcov.AsyncCacheHandler` alongside your FastAPI app.

* A stable dependency callable (from :meth:`CacheManager.get_dependency`)
  that can be used with ``Depends()`` and overridden in tests via
  ``app.dependency_overrides``.

Quickstart
----------

**app.py**::

    from contextlib import asynccontextmanager
    from typing import Annotated

    from fastapi import Depends, FastAPI
    from cashcov import AsyncCacheHandler
    from cashcov.ext.fastapi import CacheManager

    cache_manager = CacheManager(
        redis_url="redis://localhost:6379",
        prefix="myapp",
        ttl=300,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with cache_manager.lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)

    # A stable dependency callable — use this for dependency_overrides in tests.
    cache_dep = cache_manager.get_dependency()
    CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]

    @app.get("/items/{item_id}")
    async def get_item(item_id: str, cache: CacheDep):
        async def generate() -> str:
            return f"computed value for {item_id}"
        result = await cache.get_or_refresh(f"item:{item_id}", generate)
        return {"value": result.value, "from_cache": result.from_cache}

**test_app.py**::

    import pytest
    from httpx import AsyncClient, ASGITransport
    from cashcov.testing import AsyncMockCacheHandler
    from app import app, cache_dep

    @pytest.fixture
    async def client():
        mock = AsyncMockCacheHandler()
        mock.seed("item:1", "hello from cache")
        app.dependency_overrides[cache_dep] = lambda: mock
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac, mock
        app.dependency_overrides.clear()

    async def test_get_item(client):
        ac, mock = client
        resp = await ac.get("/items/1")
        assert resp.status_code == 200
        assert resp.json()["from_cache"] is True
        assert "item:1" in mock.get_or_refresh_calls
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import redis.asyncio as aioredis

from cashcov._async_handler import AsyncCacheHandler


class CacheManager:
    """Manages the lifecycle of an :class:`~cashcov.AsyncCacheHandler` for FastAPI.

    Create a single module-level instance and wire it into your app's
    ``lifespan``.  The dependency callable returned by
    :meth:`get_dependency` has a stable identity so it works correctly with
    ``app.dependency_overrides`` in tests.

    Args:
        redis_url:  Redis connection URL, e.g. ``"redis://localhost:6379"``.
        prefix:     Key namespace.
        ttl:        Default TTL in seconds.
        **kwargs:   Additional keyword arguments forwarded to
                    :class:`~cashcov.AsyncCacheHandler`.
    """

    def __init__(self, *, redis_url: str, **kwargs: Any) -> None:
        self._redis_url = redis_url
        self._handler_kwargs = kwargs
        self._handler: AsyncCacheHandler[Any] | None = None
        # The dependency function is created once here so its identity is stable
        # across calls — required for app.dependency_overrides to work.
        self._dep_fn = self._make_dep_fn()

    @asynccontextmanager
    async def lifespan(self, app: Any) -> AsyncIterator[None]:
        """Async context manager to be used inside your app's ``lifespan``.

        Creates a Redis connection and an :class:`~cashcov.AsyncCacheHandler`
        on entry; closes both on exit.  The handler is stored on
        ``app.state.cashcov`` for direct access if needed.

        Example::

            @asynccontextmanager
            async def lifespan(app: FastAPI):
                async with cache_manager.lifespan(app):
                    yield
        """
        client: aioredis.Redis = aioredis.from_url(  # type: ignore[type-arg]
            self._redis_url, decode_responses=False
        )
        self._handler = AsyncCacheHandler(client, **self._handler_kwargs)
        app.state.cashcov = self._handler
        try:
            yield
        finally:
            await self._handler.aclose()
            await client.aclose()
            self._handler = None

    def get_dependency(self) -> Callable[[], AsyncCacheHandler[Any]]:
        """Return a FastAPI dependency callable for this manager.

        The returned callable has a **stable identity** — it is created once
        in ``__init__`` and the same object is returned every time.  This
        ensures ``app.dependency_overrides[cache_dep] = ...`` works reliably.

        Example::

            cache_dep = cache_manager.get_dependency()
            CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]
        """
        return self._dep_fn

    def _make_dep_fn(self) -> Callable[[], AsyncCacheHandler[Any]]:
        manager = self

        async def _dep() -> AsyncCacheHandler[Any]:
            if manager._handler is None:
                raise RuntimeError(
                    "CacheManager has not been started. "
                    "Ensure `async with cache_manager.lifespan(app)` is called "
                    "inside your FastAPI lifespan context manager."
                )
            return manager._handler

        return _dep
