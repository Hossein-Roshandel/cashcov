"""Tests for environment-variable configuration (_config.py + from_env() classmethods)."""

from __future__ import annotations

import os

import fakeredis
import fakeredis.aioredis  # type: ignore[import-untyped]
import pytest

from cashcov import CacheHandler, AsyncCacheHandler, handler_kwargs_from_env
from cashcov._config import handler_kwargs_from_env, load_env_file
from cashcov.policies import MissFillPolicy, HitRefreshPolicy


# ---------------------------------------------------------------------------
# handler_kwargs_from_env — defaults
# ---------------------------------------------------------------------------


def test_defaults_with_no_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Without any env vars, returns library defaults."""
    # Make sure none of the CASHCOV vars are set
    for key in [
        "CASHCOV_TTL", "CASHCOV_BG_TIMEOUT", "CASHCOV_STALE_TTL",
        "CASHCOV_REFRESH_COOLDOWN", "CASHCOV_COOPERATIVE_TIMEOUT",
        "CASHCOV_REFRESH_AHEAD_THRESHOLD", "CASHCOV_PROBABILISTIC_BETA",
        "CASHCOV_MISS_DEDUP_WINDOW",
    ]:
        monkeypatch.delenv(key, raising=False)

    # Point load_env_file at a non-existent path so no .env is loaded
    monkeypatch.chdir(tmp_path)

    kwargs = handler_kwargs_from_env(load_dotenv=True)

    assert kwargs["ttl"] == 300
    assert kwargs["bg_timeout"] == 30.0
    assert kwargs["stale_ttl"] == 0
    assert kwargs["refresh_cooldown"] == 0.0
    assert kwargs["cooperative_timeout"] == 10.0
    assert kwargs["refresh_ahead_threshold"] == 0.2
    assert kwargs["probabilistic_beta"] == 1.0
    assert kwargs["dedup_window"] == 0.0


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Setting env vars produces the expected values in kwargs."""
    monkeypatch.setenv("CASHCOV_TTL", "600")
    monkeypatch.setenv("CASHCOV_BG_TIMEOUT", "5")
    monkeypatch.setenv("CASHCOV_STALE_TTL", "3600")
    monkeypatch.setenv("CASHCOV_REFRESH_COOLDOWN", "2")
    monkeypatch.setenv("CASHCOV_COOPERATIVE_TIMEOUT", "15")
    monkeypatch.setenv("CASHCOV_REFRESH_AHEAD_THRESHOLD", "0.3")
    monkeypatch.setenv("CASHCOV_PROBABILISTIC_BETA", "2.5")
    monkeypatch.setenv("CASHCOV_MISS_DEDUP_WINDOW", "10")
    monkeypatch.chdir(tmp_path)

    kwargs = handler_kwargs_from_env(load_dotenv=False)

    assert kwargs["ttl"] == 600
    assert kwargs["bg_timeout"] == 5.0
    assert kwargs["stale_ttl"] == 3600
    assert kwargs["refresh_cooldown"] == 2.0
    assert kwargs["cooperative_timeout"] == 15.0
    assert kwargs["refresh_ahead_threshold"] == 0.3
    assert kwargs["probabilistic_beta"] == 2.5
    assert kwargs["dedup_window"] == 10.0


def test_invalid_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric env var raises ValueError."""
    monkeypatch.setenv("CASHCOV_TTL", "not-a-number")
    with pytest.raises(ValueError, match="CASHCOV_TTL"):
        handler_kwargs_from_env(load_dotenv=False)


# ---------------------------------------------------------------------------
# load_env_file
# ---------------------------------------------------------------------------


def test_load_env_file_sets_variables(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "CASHCOV_TTL=120\n"
        'CASHCOV_BG_TIMEOUT="15"\n'
        "\n"  # blank line
        "CASHCOV_REFRESH_COOLDOWN=3\n"
    )
    monkeypatch.delenv("CASHCOV_TTL", raising=False)
    monkeypatch.delenv("CASHCOV_BG_TIMEOUT", raising=False)
    monkeypatch.delenv("CASHCOV_REFRESH_COOLDOWN", raising=False)

    load_env_file(env_file)

    assert os.environ["CASHCOV_TTL"] == "120"
    assert os.environ["CASHCOV_BG_TIMEOUT"] == "15"
    assert os.environ["CASHCOV_REFRESH_COOLDOWN"] == "3"


def test_load_env_file_does_not_override_existing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CASHCOV_TTL=999\n")
    monkeypatch.setenv("CASHCOV_TTL", "42")  # already set

    load_env_file(env_file)

    assert os.environ["CASHCOV_TTL"] == "42"  # not overridden


def test_load_env_file_missing_silently_ignored(tmp_path) -> None:
    """A missing .env file should not raise."""
    load_env_file(tmp_path / "nonexistent.env")  # no exception


# ---------------------------------------------------------------------------
# CacheHandler.from_env
# ---------------------------------------------------------------------------


def test_cache_handler_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """from_env() creates a CacheHandler with env-derived defaults."""
    monkeypatch.setenv("CASHCOV_TTL", "120")
    monkeypatch.setenv("CASHCOV_REFRESH_COOLDOWN", "5")
    monkeypatch.chdir(tmp_path)

    rdb = fakeredis.FakeRedis(decode_responses=False)
    with CacheHandler.from_env(
        rdb,
        prefix="test",
        hit_refresh_policy=HitRefreshPolicy.NONE,
        load_dotenv=False,
    ) as h:
        assert h._default_ttl == 120
        assert h._refresh_cooldown == 5.0
        assert h._prefix == "test"
        assert h._hit_refresh == HitRefreshPolicy.NONE
        # Basic operation works
        h.set("k", "v")
        result = h.get("k")
        assert result.value == "v"


def test_cache_handler_from_env_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Explicit kwargs override env vars."""
    monkeypatch.setenv("CASHCOV_TTL", "60")
    monkeypatch.chdir(tmp_path)

    rdb = fakeredis.FakeRedis(decode_responses=False)
    with CacheHandler.from_env(
        rdb,
        load_dotenv=False,
        ttl=999,  # explicit override
    ) as h:
        assert h._default_ttl == 999  # override wins


# ---------------------------------------------------------------------------
# AsyncCacheHandler.from_env
# ---------------------------------------------------------------------------


async def test_async_cache_handler_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AsyncCacheHandler.from_env() creates handler with env-derived defaults."""
    monkeypatch.setenv("CASHCOV_TTL", "200")
    monkeypatch.chdir(tmp_path)

    rdb = fakeredis.aioredis.FakeRedis(decode_responses=False)
    async with AsyncCacheHandler.from_env(
        rdb,
        prefix="atest",
        hit_refresh_policy=HitRefreshPolicy.NONE,
        load_dotenv=False,
    ) as h:
        assert h._default_ttl == 200
        assert h._prefix == "atest"
        await h.set("k", "v")
        result = await h.get("k")
        assert result.value == "v"
