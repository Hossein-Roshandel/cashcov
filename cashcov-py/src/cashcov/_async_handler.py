"""Asynchronous Redis cache handler built on ``redis.asyncio``.

Mirrors the API of :class:`~cashcov.CacheHandler` but every public method is
a coroutine.  Background refresh tasks are ``asyncio.Task`` objects — they
are lightweight (no threads) and are tracked so that :meth:`aclose` can
cancel and await them on shutdown.

Generator functions must be **async** callables ``async def () -> T``.  To
wrap a synchronous callable, use ``asyncio.to_thread``::

    result = await cache.get_or_refresh(
        "key",
        generator=lambda: asyncio.to_thread(sync_fn),
    )

Example::

    import redis.asyncio as aioredis
    from cashcov import AsyncCacheHandler
    from cashcov.policies import MissFillPolicy, HitRefreshPolicy

    rdb = aioredis.Redis(host="localhost", port=6379, decode_responses=False)

    async def main():
        async with AsyncCacheHandler[dict](
            rdb,
            prefix="myapp",
            ttl=300,
            miss_fill_policy=MissFillPolicy.ASYNC,
            hit_refresh_policy=HitRefreshPolicy.AHEAD,
        ) as cache:
            result = await cache.get_or_refresh(
                "user:42",
                generator=fetch_user,
            )
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Generic, TypeVar

import redis.asyncio as _aioredis

from cashcov._lock import AsyncKeyedLock
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy
from cashcov.types import CacheMissError, CacheResult

T = TypeVar("T")
AsyncGeneratorFn = Callable[[], Awaitable[T]]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal per-call options (identical to sync handler)
# ---------------------------------------------------------------------------


@dataclass
class _CallOpts:
    ttl: int | None = None
    miss_fill_policy: MissFillPolicy | None = None
    hit_refresh_policy: HitRefreshPolicy | None = None
    error_policy: ErrorPolicy | None = None
    refresh_ahead_threshold: float | None = None
    probabilistic_beta: float | None = None
    refresh_older_than: float | None = None
    disable_hit_refresh: bool = False
    stale_check_timeout: float | None = None


# ---------------------------------------------------------------------------
# AsyncCacheHandler
# ---------------------------------------------------------------------------


class AsyncCacheHandler(Generic[T]):
    """Async Redis cache handler with stampede protection and background refresh.

    See :class:`~cashcov.CacheHandler` for full parameter documentation — the
    constructor signature is identical.  The key differences are:

    * :attr:`redis_client` must be a :class:`redis.asyncio.Redis` instance.
    * All public methods are coroutines.
    * *generator* must be an ``async`` callable ``async def () -> T``.
    * Background work runs as ``asyncio.Task`` objects (no threads).
    * Use :meth:`aclose` (or ``async with``) to await background tasks on shutdown.
    """

    def __init__(
        self,
        redis_client: _aioredis.Redis,  # type: ignore[type-arg]
        *,
        prefix: str = "",
        ttl: int = 300,
        miss_fill_policy: MissFillPolicy = MissFillPolicy.SYNC,
        hit_refresh_policy: HitRefreshPolicy = HitRefreshPolicy.DEFAULT,
        error_policy: ErrorPolicy = ErrorPolicy.SURFACE,
        stale_ttl: int = 0,
        refresh_cooldown: float = 0.0,
        dedup_window: float = 0.0,
        cooperative_timeout: float = 10.0,
        refresh_ahead_threshold: float = 0.2,
        probabilistic_beta: float = 1.0,
        refresh_older_than: float = 0.0,
        bg_timeout: float = 30.0,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix
        self._default_ttl = ttl
        self._miss_fill = miss_fill_policy
        self._hit_refresh = hit_refresh_policy
        self._error_policy = error_policy
        self._stale_ttl = stale_ttl
        self._refresh_cooldown = refresh_cooldown
        self._dedup_window = dedup_window
        self._coop_timeout = cooperative_timeout
        self._ahead_threshold = refresh_ahead_threshold
        self._prob_beta = probabilistic_beta
        self._older_than = refresh_older_than
        self._bg_timeout = bg_timeout

        self._lock = AsyncKeyedLock()
        self._bg_tasks: set[asyncio.Task[None]] = set()

        # Cooldown + dedup tracking (guarded by threading.Lock for thread safety
        # in case the event loop is run from multiple threads — rare but safe)
        self._meta_lock = threading.Lock()
        self._last_write: dict[str, float] = {}
        self._gen_delta: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get(self, key: str) -> CacheResult[T]:
        """Fetch a value from Redis.

        Raises:
            KeyError: Key is not present in Redis.
        """
        full_key = self._full_key(key)
        raw = await self._redis.get(full_key)
        if raw is None:
            raise KeyError(key)
        return CacheResult(value=self._decode(raw), from_cache=True)

    async def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Write *value* to Redis with TTL (and stale shadow if configured)."""
        full_key = self._full_key(key)
        effective_ttl = ttl if ttl is not None else self._default_ttl
        await self._redis.set(full_key, self._encode(value), ex=effective_ttl)
        if self._stale_ttl > 0:
            await self._redis.set(
                full_key + ":stale",
                self._encode(value),
                ex=effective_ttl + self._stale_ttl,
            )
        self._record_write(full_key)

    async def delete(self, key: str) -> None:
        """Remove *key* (and its stale shadow) from Redis."""
        full_key = self._full_key(key)
        await self._redis.delete(full_key, full_key + ":stale")

    async def get_or_refresh(
        self,
        key: str,
        generator: AsyncGeneratorFn[T] | None = None,
        *,
        ttl: int | None = None,
        miss_fill_policy: MissFillPolicy | None = None,
        hit_refresh_policy: HitRefreshPolicy | None = None,
        error_policy: ErrorPolicy | None = None,
        refresh_ahead_threshold: float | None = None,
        probabilistic_beta: float | None = None,
        refresh_older_than: float | None = None,
        disable_hit_refresh: bool = False,
        stale_check_timeout: float | None = None,
    ) -> CacheResult[T]:
        """Async equivalent of :meth:`~cashcov.CacheHandler.get_or_refresh`.

        All parameters are identical to the sync handler; *generator* must be
        an ``async`` callable.
        """
        opts = _CallOpts(
            ttl=ttl,
            miss_fill_policy=miss_fill_policy,
            hit_refresh_policy=hit_refresh_policy,
            error_policy=error_policy,
            refresh_ahead_threshold=refresh_ahead_threshold,
            probabilistic_beta=probabilistic_beta,
            refresh_older_than=refresh_older_than,
            disable_hit_refresh=disable_hit_refresh,
            stale_check_timeout=stale_check_timeout,
        )

        effective_ttl = opts.ttl if opts.ttl is not None else self._default_ttl
        effective_miss = opts.miss_fill_policy or self._miss_fill
        if effective_miss == MissFillPolicy.DEFAULT:
            effective_miss = MissFillPolicy.SYNC
        effective_hit = (
            opts.hit_refresh_policy
            if opts.hit_refresh_policy is not None
            else self._hit_refresh
        )
        effective_err = (
            opts.error_policy if opts.error_policy is not None else self._error_policy
        )

        try:
            # 1. Cache hit
            try:
                result = await self.get(key)
                if not disable_hit_refresh:
                    await self._handle_hit_refresh(
                        key, effective_ttl, generator, effective_hit, opts
                    )
                return result
            except KeyError:
                pass

            # 2. Deduplication bypass
            if self._check_dedup(key, effective_ttl):
                try:
                    return await self.get(key)
                except KeyError:
                    pass

            # 3. Validate generator
            if generator is None and effective_miss != MissFillPolicy.FAIL_FAST:
                raise ValueError(
                    "generator is required when miss_fill_policy is not FAIL_FAST"
                )

            # 4. Dispatch fill policy
            result = await self._fill_miss(
                key, effective_ttl, generator, effective_miss, opts
            )

            if effective_hit == HitRefreshPolicy.PROBABILISTIC:
                self._record_write(self._full_key(key))

            return result

        except Exception as exc:
            if isinstance(exc, CacheMissError):
                raise
            if effective_err == ErrorPolicy.ZERO_VALUE:
                return CacheResult(value=None, from_cache=False)  # type: ignore[arg-type]
            raise

    def cached(
        self,
        key_fn: Callable[..., str],
        ttl: int | None = None,
        **call_kwargs: Any,
    ) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
        """Decorator that caches the return value of an async function.

        Example::

            @cache.cached(key_fn=lambda item_id: f"item:{item_id}", ttl=60)
            async def fetch_item(item_id: str) -> dict:
                return await db.query(item_id)
        """
        handler = self

        def decorator(
            func: Callable[..., Awaitable[T]]
        ) -> Callable[..., Awaitable[T]]:
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
        """Cancel and await all pending background tasks, then close cleanly."""
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

    async def __aenter__(self) -> AsyncCacheHandler[T]:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @classmethod
    def from_env(
        cls,
        redis_client: _aioredis.Redis,  # type: ignore[type-arg]
        *,
        prefix: str = "",
        miss_fill_policy: MissFillPolicy = MissFillPolicy.SYNC,
        hit_refresh_policy: HitRefreshPolicy = HitRefreshPolicy.DEFAULT,
        error_policy: ErrorPolicy = ErrorPolicy.SURFACE,
        refresh_older_than: float = 0.0,
        load_dotenv: bool = True,
        **overrides: Any,
    ) -> AsyncCacheHandler[T]:
        """Create an async handler with numeric defaults read from environment variables.

        Identical semantics to :meth:`~cashcov.CacheHandler.from_env`; refer
        to that method for full documentation and the list of recognised
        ``CASHCOV_*`` environment variables.

        Example::

            rdb = redis.asyncio.Redis(host="localhost", port=6379)
            handler = AsyncCacheHandler.from_env(
                rdb,
                prefix="myapp",
                hit_refresh_policy=HitRefreshPolicy.AHEAD,
            )
        """
        from cashcov._config import handler_kwargs_from_env

        kwargs = handler_kwargs_from_env(load_dotenv=load_dotenv)
        kwargs.update(overrides)
        return cls(
            redis_client,
            prefix=prefix,
            miss_fill_policy=miss_fill_policy,
            hit_refresh_policy=hit_refresh_policy,
            error_policy=error_policy,
            refresh_older_than=refresh_older_than,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}:{key}" if self._prefix else key

    def _encode(self, value: T) -> bytes:
        return json.dumps(value).encode()

    def _decode(self, raw: bytes | str) -> T:
        return json.loads(raw)  # type: ignore[return-value]

    def _record_write(self, full_key: str) -> None:
        if self._refresh_cooldown > 0 or self._dedup_window > 0:
            with self._meta_lock:
                self._last_write[full_key] = time.monotonic()

    def _record_gen_delta(self, full_key: str, delta: float) -> None:
        with self._meta_lock:
            self._gen_delta[full_key] = delta

    def _should_refresh(self, full_key: str) -> bool:
        if self._refresh_cooldown <= 0:
            return True
        with self._meta_lock:
            last = self._last_write.get(full_key)
        if last is None:
            return True
        return (time.monotonic() - last) >= self._refresh_cooldown

    def _check_dedup(self, key: str, ttl: int) -> bool:
        if self._dedup_window <= 0:
            return False
        window = min(self._dedup_window, float(ttl))
        full_key = self._full_key(key)
        with self._meta_lock:
            last = self._last_write.get(full_key)
        if last is None:
            return False
        return (time.monotonic() - last) < window

    def _spawn_bg(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule a background coroutine as a tracked Task."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------
    # Async miss fill strategies
    # ------------------------------------------------------------------

    async def _fill_miss(
        self,
        key: str,
        ttl: int,
        generator: AsyncGeneratorFn[T] | None,
        policy: MissFillPolicy,
        opts: _CallOpts,
    ) -> CacheResult[T]:
        match policy:
            case MissFillPolicy.SYNC:
                return await self._miss_sync(key, ttl, generator)  # type: ignore[arg-type]
            case MissFillPolicy.ASYNC:
                return await self._miss_async(key, ttl, generator)  # type: ignore[arg-type]
            case MissFillPolicy.STALE_OR_SYNC:
                return await self._miss_stale_or_sync(key, ttl, generator, opts)  # type: ignore[arg-type]
            case MissFillPolicy.FAIL_FAST:
                return self._miss_fail_fast(key)
            case MissFillPolicy.COOPERATIVE:
                return await self._miss_cooperative(key, ttl, generator)  # type: ignore[arg-type]
            case _:
                return await self._miss_sync(key, ttl, generator)  # type: ignore[arg-type]

    async def _miss_sync(
        self, key: str, ttl: int, generator: AsyncGeneratorFn[T]
    ) -> CacheResult[T]:
        full_key = self._full_key(key)
        async with self._lock.acquire(full_key) as acquired:
            if not acquired:
                raise RuntimeError(f"Failed to acquire lock for {key!r}")
            try:
                return await self.get(key)
            except KeyError:
                pass
            t0 = asyncio.get_event_loop().time()
            value = await generator()
            self._record_gen_delta(full_key, asyncio.get_event_loop().time() - t0)
            await self.set(key, value, ttl)
            return CacheResult(value=value, from_cache=False)

    async def _miss_async(
        self, key: str, ttl: int, generator: AsyncGeneratorFn[T]
    ) -> CacheResult[T]:
        t0 = asyncio.get_event_loop().time()
        value = await generator()
        self._record_gen_delta(self._full_key(key), asyncio.get_event_loop().time() - t0)
        self._spawn_bg(self._bg_write(key, ttl, value))
        return CacheResult(value=value, from_cache=False)

    async def _miss_stale_or_sync(
        self,
        key: str,
        ttl: int,
        generator: AsyncGeneratorFn[T],
        opts: _CallOpts,
    ) -> CacheResult[T]:
        stale_full = self._full_key(key) + ":stale"
        raw = await self._redis.get(stale_full)
        if raw is not None:
            self._spawn_bg(self._bg_refresh(key, ttl, generator))
            return CacheResult(value=self._decode(raw), from_cache=True)
        return await self._miss_sync(key, ttl, generator)

    def _miss_fail_fast(self, key: str) -> CacheResult[T]:
        raise CacheMissError(key)

    async def _miss_cooperative(
        self, key: str, ttl: int, generator: AsyncGeneratorFn[T]
    ) -> CacheResult[T]:
        full_key = self._full_key(key)
        async with self._lock.acquire(full_key, timeout=self._coop_timeout) as acquired:
            if acquired:
                try:
                    return await self.get(key)
                except KeyError:
                    pass
                t0 = asyncio.get_event_loop().time()
                value = await generator()
                self._record_gen_delta(full_key, asyncio.get_event_loop().time() - t0)
                await self.set(key, value, ttl)
                return CacheResult(value=value, from_cache=False)
            else:
                value = await generator()
                return CacheResult(value=value, from_cache=False)

    # ------------------------------------------------------------------
    # Async hit refresh strategies
    # ------------------------------------------------------------------

    async def _handle_hit_refresh(
        self,
        key: str,
        ttl: int,
        generator: AsyncGeneratorFn[T] | None,
        policy: HitRefreshPolicy,
        opts: _CallOpts,
    ) -> None:
        if generator is None or policy == HitRefreshPolicy.NONE:
            return

        full_key = self._full_key(key)

        match policy:
            case HitRefreshPolicy.DEFAULT:
                if self._should_refresh(full_key):
                    self._spawn_bg(self._bg_refresh(key, ttl, generator))

            case HitRefreshPolicy.AHEAD:
                remaining = await self._redis.ttl(full_key)
                threshold = (
                    opts.refresh_ahead_threshold
                    if opts.refresh_ahead_threshold is not None
                    else self._ahead_threshold
                )
                if remaining > 0 and (remaining / ttl) < threshold:
                    if self._should_refresh(full_key):
                        self._spawn_bg(self._bg_refresh(key, ttl, generator))

            case HitRefreshPolicy.PROBABILISTIC:
                remaining = await self._redis.ttl(full_key)
                if remaining > 0:
                    beta = (
                        opts.probabilistic_beta
                        if opts.probabilistic_beta is not None
                        else self._prob_beta
                    )
                    with self._meta_lock:
                        delta = self._gen_delta.get(full_key, 1.0)
                    if delta * beta * (-math.log(random.random())) > remaining:
                        if self._should_refresh(full_key):
                            self._spawn_bg(self._bg_refresh(key, ttl, generator))

            case HitRefreshPolicy.OLDER_THAN:
                remaining = await self._redis.ttl(full_key)
                if remaining > 0:
                    age = ttl - remaining
                    threshold = (
                        opts.refresh_older_than
                        if opts.refresh_older_than is not None
                        else self._older_than
                    )
                    if age > threshold:
                        if self._should_refresh(full_key):
                            self._spawn_bg(self._bg_refresh(key, ttl, generator))

    # ------------------------------------------------------------------
    # Background tasks (run as asyncio.Task)
    # ------------------------------------------------------------------

    async def _bg_write(self, key: str, ttl: int, value: T) -> None:
        """Background write for ASYNC miss-fill policy."""
        full_key = self._full_key(key)
        lock = await self._lock.try_acquire(full_key)
        if lock is None:
            return
        try:
            if await self._redis.exists(full_key):
                return
            await self.set(key, value, ttl)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cashcov: async background write failed for key %r", key)
        finally:
            lock.release()

    async def _bg_refresh(self, key: str, ttl: int, generator: AsyncGeneratorFn[T]) -> None:
        """Background refresh for hit-refresh policies and STALE_OR_SYNC."""
        full_key = self._full_key(key)
        lock = await self._lock.try_acquire(full_key)
        if lock is None:
            return
        try:
            if not self._should_refresh(full_key):
                return
            t0 = asyncio.get_event_loop().time()
            value = await generator()
            self._record_gen_delta(full_key, asyncio.get_event_loop().time() - t0)
            await self.set(key, value, ttl)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cashcov: async background refresh failed for key %r", key)
        finally:
            lock.release()
