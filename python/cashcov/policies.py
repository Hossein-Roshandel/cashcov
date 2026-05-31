"""cashcov.policies — Policy constants for the three independent cache axes.

These IntEnum values map 1-to-1 onto the Go iota constants in policies.go.
Pass them to :class:`~cashcov.CacheHandler` at construction time (handler-level
defaults) or to :meth:`~cashcov.CacheHandler.get_or_refresh` for per-call
overrides.

Example::

    from cashcov import CacheHandler
    from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

    handler = CacheHandler(
        redis_addr="localhost:6379",
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.2,
    )
"""

from enum import IntEnum


class MissFillPolicy(IntEnum):
    """Controls what happens on a cache miss (key not in Redis).

    Maps to Go's ``MissFillPolicy`` iota.
    """

    DEFAULT = 0
    """Zero value — falls back to SYNC at runtime."""

    SYNC = 1
    """Acquire a per-key in-process lock, double-check, generate, write, return.
    Prevents cache stampede. Highest consistency; higher miss latency."""

    ASYNC = 2
    """Generate and return immediately; write to Redis in the background.
    Lowest miss latency. Multiple concurrent callers on the *first* miss wave
    all invoke the generator — use ``dedup_window_secs`` to reduce this."""

    STALE_OR_SYNC = 3
    """Return stale (expired) data immediately if available, triggering a
    background refresh. Falls back to SYNC when no stale data exists.
    Requires ``stale_ttl_secs > 0`` on the handler."""

    FAIL_FAST = 4
    """Return ``CacheError`` immediately without calling the generator.
    Intended for circuit-breaker or explicit-fallback patterns."""

    COOPERATIVE = 5
    """First caller acquires the lock and generates; all other concurrent callers
    block until the lock is released or ``cooperative_timeout_secs`` elapses,
    then fall back to direct generation without caching."""


class HitRefreshPolicy(IntEnum):
    """Controls proactive background refresh when the key *is* in the cache.

    Maps to Go's ``HitRefreshPolicy`` iota.
    """

    DEFAULT = 0
    """Standard background refresh on every hit, gated by ``refresh_cooldown_secs``."""

    AHEAD = 1
    """Trigger a background refresh when the remaining Redis TTL drops below
    ``refresh_ahead_threshold`` × original TTL (e.g. 20%)."""

    PROBABILISTIC = 2
    """XFetch algorithm: refresh probability increases continuously as the entry
    ages. Distributes refresh load without coordination. Tune with
    ``probabilistic_beta``."""

    OLDER_THAN = 3
    """Trigger a background refresh when the entry's age (original TTL minus
    remaining TTL) exceeds ``refresh_older_than_secs``."""

    NONE = 4
    """Disable all background refresh on cache hits."""


class ErrorPolicy(IntEnum):
    """Controls how generator errors are surfaced to the caller.

    Maps to Go's ``ErrorPolicy`` iota.
    """

    SURFACE = 0
    """Return the generator error to the caller (default)."""

    ZERO_VALUE = 1
    """Suppress generator errors — caller receives ``CacheError`` only for
    ``FAIL_FAST`` misses; all other errors are silently swallowed and
    ``get_or_refresh`` returns ``None``."""
