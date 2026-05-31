"""In-process mock cache handlers for unit testing.

Drop-in replacements for :class:`~cashcov.CacheHandler` and
:class:`~cashcov.AsyncCacheHandler` backed by an in-memory ``dict``.  No Redis
connection required.

Features
--------
* **Seed** pre-existing values with :meth:`seed`.
* **Inspect calls** via :attr:`get_calls`, :attr:`set_calls`,
  :attr:`get_or_refresh_calls`.
* **Simulate errors** for specific keys with :meth:`inject_error`.
* **Force a miss** for specific keys with :meth:`force_miss`.
* **Reset** everything with :meth:`reset`.
* Compatible with FastAPI ``dependency_overrides``.

Example (sync)::

    from cashcov.testing import MockCacheHandler

    def test_product_route():
        mock = MockCacheHandler[dict]()
        mock.seed("product:p1", {"id": "p1", "name": "Widget", "price": 9.99})

        result = mock.get_or_refresh("product:p1", generator=lambda: {})
        assert result.from_cache
        assert result.value["name"] == "Widget"
        assert mock.get_or_refresh_calls == ["product:p1"]

Example (async + FastAPI)::

    from cashcov.testing import AsyncMockCacheHandler

    async def test_product_endpoint(client: AsyncClient):
        mock = AsyncMockCacheHandler[dict]()
        mock.seed("product:p1", {"id": "p1", "name": "Widget"})
        app.dependency_overrides[cache_dep] = lambda: mock

        resp = await client.get("/products/p1")
        assert resp.status_code == 200

        app.dependency_overrides.clear()
"""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

from cashcov.types import CacheMissError, CacheResult

T = TypeVar("T")


class MockCacheHandler(Generic[T]):
    """Synchronous in-memory mock of :class:`~cashcov.CacheHandler`.

    All ``get_or_refresh`` call kwargs (``ttl``, policy overrides, etc.) are
    accepted but silently ignored — the mock always checks its in-memory store.
    """

    def __init__(self) -> None:
        self._store: dict[str, T] = {}
        self._errors: dict[str, Exception] = {}
        self._forced_misses: set[str] = set()

        # Call history — inspect these in your assertions
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, T]] = []
        self.delete_calls: list[str] = []
        self.get_or_refresh_calls: list[str] = []

    # ------------------------------------------------------------------
    # Configuration helpers (fluent API)
    # ------------------------------------------------------------------

    def seed(self, key: str, value: T) -> MockCacheHandler[T]:
        """Pre-load *key* → *value* into the mock store.

        Returns ``self`` for chaining::

            mock.seed("a", 1).seed("b", 2)
        """
        self._store[key] = value
        return self

    def inject_error(self, key: str, exc: Exception) -> MockCacheHandler[T]:
        """Make :meth:`get_or_refresh` raise *exc* whenever *key* is requested."""
        self._errors[key] = exc
        return self

    def force_miss(self, *keys: str) -> MockCacheHandler[T]:
        """Make :meth:`get` and :meth:`get_or_refresh` always treat *keys* as absent.

        The generator is still called, so the seeded value is not used.
        """
        self._forced_misses.update(keys)
        return self

    def reset(self) -> MockCacheHandler[T]:
        """Clear all stored data, errors, forced misses, and call history."""
        self._store.clear()
        self._errors.clear()
        self._forced_misses.clear()
        self.get_calls.clear()
        self.set_calls.clear()
        self.delete_calls.clear()
        self.get_or_refresh_calls.clear()
        return self

    # ------------------------------------------------------------------
    # CacheHandler interface
    # ------------------------------------------------------------------

    def get(self, key: str) -> CacheResult[T]:
        """Return the seeded value or raise ``KeyError``."""
        self.get_calls.append(key)
        if key in self._forced_misses or key not in self._store:
            raise KeyError(key)
        return CacheResult(value=self._store[key], from_cache=True)

    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Store *value* in the mock store."""
        self.set_calls.append((key, value))
        self._store[key] = value

    def delete(self, key: str) -> None:
        """Remove *key* from the mock store."""
        self.delete_calls.append(key)
        self._store.pop(key, None)

    def get_or_refresh(
        self,
        key: str,
        generator: Callable[[], T] | None = None,
        **_kwargs: Any,
    ) -> CacheResult[T]:
        """Return the seeded value or call *generator* on a miss.

        Raises:
            Exception:     If an error was injected via :meth:`inject_error`.
            CacheMissError: If no generator is provided and the key is absent.
        """
        self.get_or_refresh_calls.append(key)

        if key in self._errors:
            raise self._errors[key]

        if key in self._forced_misses or key not in self._store:
            if generator is None:
                raise CacheMissError(key)
            value = generator()
            self._store[key] = value
            return CacheResult(value=value, from_cache=False)

        return CacheResult(value=self._store[key], from_cache=True)

    def cached(
        self,
        key_fn: Callable[..., str],
        ttl: int | None = None,
        **call_kwargs: Any,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """No-op decorator stub — wraps the function transparently."""
        import functools

        handler = self

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> T:
                key = key_fn(*args, **kwargs)

                def generator() -> T:
                    return func(*args, **kwargs)

                result = handler.get_or_refresh(key, generator, ttl=ttl, **call_kwargs)
                return result.value

            return wrapper

        return decorator

    def close(self) -> None:
        """No-op — included for interface compatibility."""

    def __enter__(self) -> MockCacheHandler[T]:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"MockCacheHandler(keys={list(self._store)}, "
            f"calls={len(self.get_or_refresh_calls)})"
        )


# ---------------------------------------------------------------------------
# Async mock
# ---------------------------------------------------------------------------


class AsyncMockCacheHandler(Generic[T]):
    """Async in-memory mock of :class:`~cashcov.AsyncCacheHandler`.

    Implements the full async interface so it can be used as a drop-in with
    FastAPI dependency injection.

    All configuration helpers (:meth:`seed`, :meth:`inject_error`,
    :meth:`force_miss`, :meth:`reset`) are identical to
    :class:`MockCacheHandler`.
    """

    def __init__(self) -> None:
        self._store: dict[str, T] = {}
        self._errors: dict[str, Exception] = {}
        self._forced_misses: set[str] = set()

        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, T]] = []
        self.delete_calls: list[str] = []
        self.get_or_refresh_calls: list[str] = []

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def seed(self, key: str, value: T) -> AsyncMockCacheHandler[T]:
        """Pre-load *key* → *value* (fluent API)."""
        self._store[key] = value
        return self

    def inject_error(self, key: str, exc: Exception) -> AsyncMockCacheHandler[T]:
        """Make ``get_or_refresh`` raise *exc* whenever *key* is requested."""
        self._errors[key] = exc
        return self

    def force_miss(self, *keys: str) -> AsyncMockCacheHandler[T]:
        """Always treat *keys* as absent."""
        self._forced_misses.update(keys)
        return self

    def reset(self) -> AsyncMockCacheHandler[T]:
        """Clear all state and call history."""
        self._store.clear()
        self._errors.clear()
        self._forced_misses.clear()
        self.get_calls.clear()
        self.set_calls.clear()
        self.delete_calls.clear()
        self.get_or_refresh_calls.clear()
        return self

    # ------------------------------------------------------------------
    # AsyncCacheHandler interface
    # ------------------------------------------------------------------

    async def get(self, key: str) -> CacheResult[T]:
        self.get_calls.append(key)
        if key in self._forced_misses or key not in self._store:
            raise KeyError(key)
        return CacheResult(value=self._store[key], from_cache=True)

    async def set(self, key: str, value: T, ttl: int | None = None) -> None:
        self.set_calls.append((key, value))
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        self._store.pop(key, None)

    async def get_or_refresh(
        self,
        key: str,
        generator: Callable[[], Any] | None = None,
        **_kwargs: Any,
    ) -> CacheResult[T]:
        """Async equivalent of :meth:`MockCacheHandler.get_or_refresh`.

        If *generator* is a coroutine function it is awaited; otherwise it is
        called synchronously.
        """
        import asyncio
        import inspect

        self.get_or_refresh_calls.append(key)

        if key in self._errors:
            raise self._errors[key]

        if key in self._forced_misses or key not in self._store:
            if generator is None:
                raise CacheMissError(key)
            if inspect.iscoroutinefunction(generator):
                value = await generator()
            else:
                value = generator()
            self._store[key] = value
            return CacheResult(value=value, from_cache=False)

        return CacheResult(value=self._store[key], from_cache=True)

    def cached(
        self,
        key_fn: Callable[..., str],
        ttl: int | None = None,
        **call_kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """No-op decorator stub — wraps the function transparently."""
        import functools

        handler = self

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> T:
                key = key_fn(*args, **kwargs)

                async def generator() -> T:
                    return await func(*args, **kwargs)

                result = await handler.get_or_refresh(
                    key, generator, ttl=ttl, **call_kwargs
                )
                return result.value

            return wrapper

        return decorator

    async def aclose(self) -> None:
        """No-op — included for interface compatibility."""

    async def __aenter__(self) -> AsyncMockCacheHandler[T]:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AsyncMockCacheHandler(keys={list(self._store)}, "
            f"calls={len(self.get_or_refresh_calls)})"
        )
