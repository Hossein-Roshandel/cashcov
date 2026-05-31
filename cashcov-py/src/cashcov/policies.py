"""Cache policy enumerations — three independent axes of cache behaviour.

Every axis can be configured at handler level (default) and overridden
per-call via :meth:`~cashcov.CacheHandler.get_or_refresh`.

Example::

    from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

    handler = CacheHandler(
        redis_client,
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.2,
    )
"""

from __future__ import annotations

from enum import IntEnum


class MissFillPolicy(IntEnum):
    """Controls what happens on a cache **miss** (key absent from Redis)."""

    DEFAULT = 0
    """Zero value — resolved to :attr:`SYNC` at runtime."""

    SYNC = 1
    """Acquire a per-key in-process lock, double-check Redis, call the
    generator, write to Redis, then return.  Prevents cache stampede.
    Highest consistency; highest miss latency."""

    ASYNC = 2
    """Call the generator and return immediately; write to Redis in the
    background.  Lowest miss latency.  Concurrent callers on the *first*
    miss wave all invoke the generator — use ``dedup_window`` to suppress
    duplicate generation after the first write within a configurable window."""

    STALE_OR_SYNC = 3
    """Return stale (expired) data immediately when available, triggering a
    background refresh.  Falls back to :attr:`SYNC` when no stale data
    exists.  Requires ``stale_ttl > 0`` on the handler; without it the stale
    lookup always misses and this behaves identically to :attr:`SYNC`."""

    FAIL_FAST = 4
    """Raise :exc:`~cashcov.CacheMissError` immediately without calling the
    generator.  Intended for circuit-breaker or explicit-fallback patterns."""

    COOPERATIVE = 5
    """First concurrent caller acquires the per-key lock and generates the
    value; all other callers for the same key block until the lock is
    released or ``cooperative_timeout`` elapses, at which point they fall
    back to direct generation without caching."""


class HitRefreshPolicy(IntEnum):
    """Controls proactive background refresh when the key **is** in the cache."""

    DEFAULT = 0
    """Standard background refresh on every hit, gated by ``refresh_cooldown``."""

    AHEAD = 1
    """Trigger a background refresh when the remaining Redis TTL drops below
    ``refresh_ahead_threshold`` × original TTL (e.g. refresh when 20 % TTL
    remains).  Configure the threshold with ``refresh_ahead_threshold``."""

    PROBABILISTIC = 2
    """XFetch algorithm: the probability of an early refresh increases
    continuously as the entry ages, distributing refresh load across requests
    without coordination.  Tune sensitivity with ``probabilistic_beta``."""

    OLDER_THAN = 3
    """Trigger a background refresh when the entry's age (original TTL minus
    remaining TTL) exceeds ``refresh_older_than`` seconds."""

    NONE = 4
    """Disable all background refresh on cache hits."""


class ErrorPolicy(IntEnum):
    """Controls how a generator error is surfaced to the caller."""

    SURFACE = 0
    """Re-raise the exception.  This is the default and zero value."""

    ZERO_VALUE = 1
    """Suppress the error and return
    ``CacheResult(value=None, from_cache=False)`` silently.  Useful when
    degraded operation is acceptable."""
