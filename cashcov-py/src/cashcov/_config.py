"""Environment-variable configuration loader for cashcov.

Mirrors the Go ``loadHandlerConfig`` approach: reads numeric tuning parameters
from the process environment (and optionally a ``.env`` file) so that
deployment-level defaults can be controlled without code changes.

Environment variables
---------------------

All variables are optional.  Unset variables fall back to the library defaults.

+-------------------------------------+---------+------------+--------------+
| Variable                            | Default | Unit       | Go equivalent|
+=====================================+=========+============+==============+
| ``CASHCOV_TTL``                     | 300     | seconds    | CACHE_DEFAULT_TTL_MINUTES × 60 |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_BG_TIMEOUT``              | 30      | seconds    | CACHE_BG_REFRESH_TIMEOUT_SECONDS |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_STALE_TTL``               | 0       | seconds    | CACHE_STALE_DATA_TTL_HOURS × 3600 |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_REFRESH_COOLDOWN``        | 0       | seconds    | CACHE_BG_REFRESH_COOLDOWN_SECONDS |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_COOPERATIVE_TIMEOUT``     | 10      | seconds    | CACHE_COOPERATIVE_TIMEOUT_SECONDS |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_REFRESH_AHEAD_THRESHOLD`` | 0.2     | fraction   | CACHE_REFRESH_AHEAD_THRESHOLD |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_PROBABILISTIC_BETA``      | 1.0     | multiplier | CACHE_DEFAULT_PROBABILISTIC_BETA |
+-------------------------------------+---------+------------+--------------+
| ``CASHCOV_MISS_DEDUP_WINDOW``       | 0       | seconds    | missDeduplicationWindow |
+-------------------------------------+---------+------------+--------------+

Usage::

    from cashcov import CacheHandler
    import redis

    rdb = redis.Redis(host="localhost", port=6379, decode_responses=False)

    # Pick up all numeric defaults from environment / .env
    handler = CacheHandler.from_env(rdb, prefix="myapp")

    # Or merge manually
    from cashcov._config import handler_kwargs_from_env
    kwargs = handler_kwargs_from_env()
    kwargs["prefix"] = "myapp"
    handler = CacheHandler(rdb, **kwargs)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# .env file loader
# ---------------------------------------------------------------------------


def load_env_file(path: str | Path = ".env") -> None:
    """Load ``KEY=VALUE`` pairs from *path* into ``os.environ``.

    * Lines starting with ``#`` and blank lines are ignored.
    * Values may be surrounded by single or double quotes (stripped).
    * Already-set variables are **not** overridden (same semantics as
      ``python-dotenv`` ``load_dotenv(override=False)``).
    * Silently ignores a missing file.

    Args:
        path: Path to the ``.env`` file.  Defaults to ``.env`` in the
              current working directory.
    """
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Individual variable parsers
# ---------------------------------------------------------------------------


def _read_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"cashcov env var {name!r}: expected a float, got {raw!r}"
        ) from exc
    if not allow_zero and v < 0:
        raise ValueError(
            f"cashcov env var {name!r}: expected >= 0, got {v}"
        )
    if not allow_zero and v == 0:
        # Only raise if strictly positive is required
        pass  # zero is allowed for cooldown / stale_ttl / dedup_window
    return v


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def handler_kwargs_from_env(*, load_dotenv: bool = True) -> dict[str, Any]:
    """Return ``CacheHandler`` / ``AsyncCacheHandler`` keyword arguments from env.

    Numeric tuning parameters are read from environment variables; policy
    enums and non-numeric options must still be passed explicitly.

    Args:
        load_dotenv: If ``True`` (default), attempt to load a ``.env`` file
                     from the current working directory before reading env vars.

    Returns:
        Dict of keyword arguments ready to spread into a handler constructor::

            handler = CacheHandler(rdb, **handler_kwargs_from_env())

    Raises:
        ValueError: If an environment variable is set to an invalid value.
    """
    if load_dotenv:
        load_env_file()

    return {
        "ttl": int(_read_float("CASHCOV_TTL", 300.0, allow_zero=False)),
        "bg_timeout": _read_float("CASHCOV_BG_TIMEOUT", 30.0, allow_zero=False),
        "stale_ttl": int(_read_float("CASHCOV_STALE_TTL", 0.0, allow_zero=True)),
        "refresh_cooldown": _read_float(
            "CASHCOV_REFRESH_COOLDOWN", 0.0, allow_zero=True
        ),
        "cooperative_timeout": _read_float(
            "CASHCOV_COOPERATIVE_TIMEOUT", 10.0, allow_zero=False
        ),
        "refresh_ahead_threshold": _read_float(
            "CASHCOV_REFRESH_AHEAD_THRESHOLD", 0.2, allow_zero=False
        ),
        "probabilistic_beta": _read_float(
            "CASHCOV_PROBABILISTIC_BETA", 1.0, allow_zero=False
        ),
        "dedup_window": _read_float(
            "CASHCOV_MISS_DEDUP_WINDOW", 0.0, allow_zero=True
        ),
    }
