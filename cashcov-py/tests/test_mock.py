"""Tests for MockCacheHandler and AsyncMockCacheHandler."""

from __future__ import annotations

import pytest

from cashcov import CacheMissError
from cashcov.testing import AsyncMockCacheHandler, MockCacheHandler


# ---------------------------------------------------------------------------
# MockCacheHandler (sync)
# ---------------------------------------------------------------------------


def test_mock_seed_and_hit() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.seed("k", "hello")

    result = mock.get_or_refresh("k")
    assert result.value == "hello"
    assert result.from_cache is True
    assert mock.get_or_refresh_calls == ["k"]


def test_mock_miss_calls_generator() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()

    result = mock.get_or_refresh("k", generator=lambda: "generated")
    assert result.value == "generated"
    assert result.from_cache is False


def test_mock_miss_without_generator_raises() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()

    with pytest.raises(CacheMissError):
        mock.get_or_refresh("absent")


def test_mock_inject_error() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.inject_error("k", RuntimeError("DB down"))

    with pytest.raises(RuntimeError, match="DB down"):
        mock.get_or_refresh("k", generator=lambda: "v")


def test_mock_force_miss() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.seed("k", "cached").force_miss("k")

    result = mock.get_or_refresh("k", generator=lambda: "fresh")
    assert result.value == "fresh"
    assert result.from_cache is False


def test_mock_set_and_get() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.set("k", "v")

    result = mock.get("k")
    assert result.value == "v"
    assert mock.set_calls == [("k", "v")]


def test_mock_delete() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.seed("k", "v")
    mock.delete("k")

    with pytest.raises(KeyError):
        mock.get("k")
    assert "k" in mock.delete_calls


def test_mock_reset() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    mock.seed("k", "v")
    mock.get_or_refresh("k")
    mock.reset()

    assert mock.get_or_refresh_calls == []
    assert mock.get_calls == []
    assert mock.set_calls == []

    with pytest.raises(CacheMissError):
        mock.get_or_refresh("k")


def test_mock_chaining() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()
    result = mock.seed("a", "1").seed("b", "2").get_or_refresh("a")
    assert result.value == "1"


def test_mock_context_manager() -> None:
    with MockCacheHandler[str]() as mock:
        mock.seed("k", "v")
        result = mock.get_or_refresh("k")
    assert result.value == "v"


def test_mock_cached_decorator() -> None:
    mock: MockCacheHandler[str] = MockCacheHandler()

    @mock.cached(key_fn=lambda x: f"item:{x}")
    def fetch(x: str) -> str:
        return f"computed:{x}"

    assert fetch("a") == "computed:a"  # miss → generator
    assert fetch("a") == "computed:a"  # hit → cached value returned
    assert mock.get_or_refresh_calls.count("item:a") == 2


# ---------------------------------------------------------------------------
# AsyncMockCacheHandler
# ---------------------------------------------------------------------------


async def test_async_mock_seed_and_hit() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()
    mock.seed("k", "hello")

    result = await mock.get_or_refresh("k")
    assert result.value == "hello"
    assert result.from_cache is True


async def test_async_mock_miss_calls_async_generator() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()

    async def gen() -> str:
        return "async-generated"

    result = await mock.get_or_refresh("k", generator=gen)
    assert result.value == "async-generated"
    assert result.from_cache is False


async def test_async_mock_miss_calls_sync_generator() -> None:
    """AsyncMockCacheHandler also accepts sync generators."""
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()

    result = await mock.get_or_refresh("k", generator=lambda: "sync")
    assert result.value == "sync"


async def test_async_mock_inject_error() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()
    mock.inject_error("k", ValueError("broken"))

    with pytest.raises(ValueError, match="broken"):
        await mock.get_or_refresh("k", generator=lambda: "v")


async def test_async_mock_force_miss() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()
    mock.seed("k", "stale").force_miss("k")

    result = await mock.get_or_refresh("k", generator=lambda: "fresh")
    assert result.value == "fresh"


async def test_async_mock_reset() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()
    mock.seed("k", "v")
    await mock.get_or_refresh("k")
    mock.reset()

    assert mock.get_or_refresh_calls == []
    with pytest.raises(CacheMissError):
        await mock.get_or_refresh("k")


async def test_async_mock_context_manager() -> None:
    async with AsyncMockCacheHandler[str]() as mock:
        mock.seed("k", "v")
        result = await mock.get_or_refresh("k")
    assert result.value == "v"


async def test_async_mock_cached_decorator() -> None:
    mock: AsyncMockCacheHandler[str] = AsyncMockCacheHandler()

    @mock.cached(key_fn=lambda x: f"item:{x}")
    async def fetch(x: str) -> str:
        return f"computed:{x}"

    assert await fetch("a") == "computed:a"
    assert await fetch("a") == "computed:a"  # hit
    assert mock.get_or_refresh_calls.count("item:a") == 2
