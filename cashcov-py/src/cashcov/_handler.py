"""Synchronous Redis cache handler.

All values are JSON-serialised so ``T`` must be JSON-serialisable (dicts,
lists, primitives, Pydantic models via ``.model_dump()``, dataclasses via
``dataclasses.asdict()``, etc.).

Example::

    import redis
    from cashcov import CacheHandler
    from cashcov.policies import MissFillPolicy, HitRefreshPolicy

    rdb = redis.Redis(host="localhost", port=6379, decode_responses=False)

    with CacheHandler[dict](
        rdb,
        prefix="myapp",
        ttl=300,
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.2,
    ) as cache:

        result = cache.get_or_refresh(
            "user:42",
            generator=lambda: fetch_user(42),
        )
        print(result.value, result.from_cache)
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

import redis as _redis_module

from cashcov._lock import KeyedLock
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy
from cashcov.types import CacheMissError, CacheResult

T = TypeVar("T")
GeneratorFn = Callable[[], T]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal per-call options
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
# CacheHandler
# ---------------------------------------------------------------------------


class CacheHandler(Generic[T]):
    """Synchronous Redis cache handler with stampede protection and background refresh.

    Three independent policy axes (configurable at handler level, overridable
    per-call):

    * **MissFillPolicy** — what to do when the key is absent from Redis.
    * **HitRefreshPolicy** — when to proactively refresh on a cache hit.
    * **ErrorPolicy** — how to surface generator errors.

    All values are exchanged as JSON strings.

    Args:
        redis_client:            A connected :class:`redis.Redis` instance
                                 (``decode_responses=False``).
        prefix:                  Key namespace, e.g. ``"myapp"``.
        ttl:                     Default TTL in seconds.
        miss_fill_policy:        Handler-level miss-fill strategy.
        hit_refresh_policy:      Handler-level hit-refresh strategy.
        error_policy:            Handler-level error policy.
        stale_ttl:               Extra seconds beyond ``ttl`` to keep a stale
                                 shadow key for :attr:`~MissFillPolicy.STALE_OR_SYNC`.
        refresh_cooldown:        Minimum seconds between background refreshes
                                 for the same key (hit-path only).
        dedup_window:            Seconds during which a second miss for the same
                                 key skips the generator and retries Redis
                                 (:attr:`~MissFillPolicy.ASYNC` stampede guard).
        cooperative_timeout:     Seconds other callers wait under
                                 :attr:`~MissFillPolicy.COOPERATIVE`.
        refresh_ahead_threshold: Fraction of TTL remaining that triggers
                                 :attr:`~HitRefreshPolicy.AHEAD` refresh
                                 (e.g. ``0.2`` = refresh when 20 % TTL remains).
        probabilistic_beta:      Sensitivity for
                                 :attr:`~HitRefreshPolicy.PROBABILISTIC`
                                 (default ``1.0``).
        refresh_older_than:      Entry age in seconds that triggers
                                 :attr:`~HitRefreshPolicy.OLDER_THAN` refresh.
        bg_timeout:              Seconds a background worker is allowed to run.
        max_bg_workers:          Thread-pool size for background tasks.
    """

    def __init__(
        self,
        redis_client: _redis_module.Redis,  # type: ignore[type-arg]
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
        max_bg_workers: int = 10,
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

        self._lock = KeyedLock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_bg_workers,
            thread_name_prefix="cashcov-bg",
        )

        # Cooldown + dedup tracking: full_key → monotonic write time
        self._meta_lock = threading.Lock()
        self._last_write: dict[str, float] = {}
        # XFetch: track measured generation time per key
        self._gen_delta: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str) -> CacheResult[T]:
        """Fetch a value from Redis.

        Raises:
            KeyError: Key is not present in Redis.
        """
        full_key = self._full_key(key)
        raw = self._redis.get(full_key)
        if raw is None:
            raise KeyError(key)
        return CacheResult(value=self._decode(raw), from_cache=True)

    def set(self, key: str, value: T, ttl: int | None = None) -> None:
        """Write *value* to Redis with TTL.

        When ``stale_ttl > 0`` a shadow key ``{key}:stale`` is also written
        with an extended TTL of ``ttl + stale_ttl`` for use with
        :attr:`~MissFillPolicy.STALE_OR_SYNC`.
        """
        full_key = self._full_key(key)
        effective_ttl = ttl if ttl is not None else self._default_ttl
        self._redis.set(full_key, self._encode(value), ex=effective_ttl)
        if self._stale_ttl > 0:
            self._redis.set(
                full_key + ":stale",
                self._encode(value),
                ex=effective_ttl + self._stale_ttl,
            )
        self._record_write(full_key)

    def delete(self, key: str) -> None:
        """Remove *key* (and its stale shadow) from Redis."""
        full_key = self._full_key(key)
        self._redis.delete(full_key, full_key + ":stale")

    def get_or_refresh(
        self,
        key: str,
        generator: GeneratorFn[T] | None = None,
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
        """Fetch from Redis or call *generator* on a miss.

        Args:
            key:                     Cache key (prefix applied automatically).
            generator:               ``() -> T`` callable.  Required unless
                                     ``miss_fill_policy=FAIL_FAST``.
            ttl:                     Per-call TTL override (seconds).
            miss_fill_policy:        Per-call miss-fill override.
            hit_refresh_policy:      Per-call hit-refresh override.
            error_policy:            Per-call error-policy override.
            refresh_ahead_threshold: Per-call AHEAD threshold override.
            probabilistic_beta:      Per-call XFetch beta override.
            refresh_older_than:      Per-call OLDER_THAN age override (seconds).
            disable_hit_refresh:     Skip background refresh even on hit.
            stale_check_timeout:     Seconds to wait when checking the stale key.

        Returns:
            :class:`~cashcov.types.CacheResult` with the value and metadata.

        Raises:
            CacheMissError: When ``miss_fill_policy=FAIL_FAST`` and the key is absent.
            ValueError:     When no generator is provided and the policy needs one.
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
                result = self.get(key)
                if not disable_hit_refresh:
                    self._handle_hit_refresh(key, effective_ttl, generator, effective_hit, opts)
                return result
            except KeyError:
                pass

            # 2. Deduplication bypass: if this process recently wrote the key,
            #    retry Redis before calling the generator.
            if self._check_dedup(key, effective_ttl):
                try:
                    return self.get(key)
                except KeyError:
                    pass  # Key gone already; fall through to fill policy

            # 3. Miss: validate generator
            if generator is None and effective_miss != MissFillPolicy.FAIL_FAST:
                raise ValueError(
                    "generator is required when miss_fill_policy is not FAIL_FAST"
                )

            # 4. Dispatch fill policy
            result = self._fill_miss(key, effective_ttl, generator, effective_miss, opts)

            # Track creation time for probabilistic refresh
            if effective_hit == HitRefreshPolicy.PROBABILISTIC:
                self._record_write(self._full_key(key))

            return result

        except Exception as exc:
            if isinstance(exc, CacheMissError):
                raise  # Never suppress CacheMissError — it's an intentional signal
            if effective_err == ErrorPolicy.ZERO_VALUE:
                return CacheResult(value=None, from_cache=False)  # type: ignore[arg-type]
            raise

    def cached(
        self,
        key_fn: Callable[..., str],
        ttl: int | None = None,
        **call_kwargs: Any,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator that caches the return value of a function.

        Args:
            key_fn:  Called with the same args as the decorated function to
                     produce the cache key.
            ttl:     TTL override for this decorator (seconds).
            **call_kwargs: Forwarded to :meth:`get_or_refresh` as keyword args.

        Example::

            @cache.cached(key_fn=lambda item_id: f"item:{item_id}", ttl=60)
            def fetch_item(item_id: str) -> dict:
                return db.query(item_id)
        """
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
        """Shut down the background thread pool.

        Pending futures are cancelled; already-running workers are allowed to
        finish.  Call this when the handler is no longer needed.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> CacheHandler[T]:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @classmethod
    def from_env(
        cls,
        redis_client: _redis_module.Redis,  # type: ignore[type-arg]
        *,
        prefix: str = "",
        miss_fill_policy: MissFillPolicy = MissFillPolicy.SYNC,
        hit_refresh_policy: HitRefreshPolicy = HitRefreshPolicy.DEFAULT,
        error_policy: ErrorPolicy = ErrorPolicy.SURFACE,
        refresh_older_than: float = 0.0,
        max_bg_workers: int = 10,
        load_dotenv: bool = True,
        **overrides: Any,
    ) -> CacheHandler[T]:
        """Create a handler with numeric defaults read from environment variables.

        Loads ``CASHCOV_*`` env vars (and optionally a ``.env`` file), then
        applies any explicit *overrides*.  Policy enums and non-numeric options
        (``prefix``, ``miss_fill_policy``, etc.) must still be passed directly.

        Recognised env vars (all optional):

        * ``CASHCOV_TTL`` — default TTL in seconds (default: 300)
        * ``CASHCOV_BG_TIMEOUT`` — background task timeout in seconds (default: 30)
        * ``CASHCOV_STALE_TTL`` — stale shadow key extra seconds (default: 0)
        * ``CASHCOV_REFRESH_COOLDOWN`` — min seconds between bg refreshes (default: 0)
        * ``CASHCOV_COOPERATIVE_TIMEOUT`` — seconds to wait under COOPERATIVE (default: 10)
        * ``CASHCOV_REFRESH_AHEAD_THRESHOLD`` — AHEAD fraction (default: 0.2)
        * ``CASHCOV_PROBABILISTIC_BETA`` — XFetch beta (default: 1.0)
        * ``CASHCOV_MISS_DEDUP_WINDOW`` — dedup window in seconds (default: 0)

        Example::

            # .env
            # CASHCOV_TTL=600
            # CASHCOV_REFRESH_COOLDOWN=5

            handler = CacheHandler.from_env(
                redis_client,
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
            max_bg_workers=max_bg_workers,
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
        """Return True if this process wrote the key within the dedup window."""
        if self._dedup_window <= 0:
            return False
        window = min(self._dedup_window, float(ttl))
        full_key = self._full_key(key)
        with self._meta_lock:
            last = self._last_write.get(full_key)
        if last is None:
            return False
        return (time.monotonic() - last) < window

    # ------------------------------------------------------------------
    # Miss fill strategies
    # ------------------------------------------------------------------

    def _fill_miss(
        self,
        key: str,
        ttl: int,
        generator: GeneratorFn[T] | None,
        policy: MissFillPolicy,
        opts: _CallOpts,
    ) -> CacheResult[T]:
        match policy:
            case MissFillPolicy.SYNC:
                return self._miss_sync(key, ttl, generator)  # type: ignore[arg-type]
            case MissFillPolicy.ASYNC:
                return self._miss_async(key, ttl, generator)  # type: ignore[arg-type]
            case MissFillPolicy.STALE_OR_SYNC:
                return self._miss_stale_or_sync(key, ttl, generator, opts)  # type: ignore[arg-type]
            case MissFillPolicy.FAIL_FAST:
                return self._miss_fail_fast(key)
            case MissFillPolicy.COOPERATIVE:
                return self._miss_cooperative(key, ttl, generator)  # type: ignore[arg-type]
            case _:
                return self._miss_sync(key, ttl, generator)  # type: ignore[arg-type]

    def _miss_sync(self, key: str, ttl: int, generator: GeneratorFn[T]) -> CacheResult[T]:
        """Lock → double-check → generate → write → return."""
        full_key = self._full_key(key)
        with self._lock.acquire(full_key) as acquired:
            if not acquired:
                raise RuntimeError(f"Failed to acquire lock for {key!r}")  # shouldn't happen
            # Double-check after acquiring lock
            try:
                return self.get(key)
            except KeyError:
                pass
            t0 = time.monotonic()
            value = generator()
            self._record_gen_delta(full_key, time.monotonic() - t0)
            self.set(key, value, ttl)
            return CacheResult(value=value, from_cache=False)

    def _miss_async(self, key: str, ttl: int, generator: GeneratorFn[T]) -> CacheResult[T]:
        """Generate → return immediately → background write."""
        t0 = time.monotonic()
        value = generator()
        self._record_gen_delta(self._full_key(key), time.monotonic() - t0)
        self._executor.submit(self._bg_write, key, ttl, value)
        return CacheResult(value=value, from_cache=False)

    def _miss_stale_or_sync(
        self,
        key: str,
        ttl: int,
        generator: GeneratorFn[T],
        opts: _CallOpts,
    ) -> CacheResult[T]:
        """Return stale data if available; otherwise fall back to SYNC."""
        stale_full = self._full_key(key) + ":stale"
        raw = self._redis.get(stale_full)
        if raw is not None:
            # Stale data found — return immediately and refresh in the background
            self._executor.submit(self._bg_refresh, key, ttl, generator)
            return CacheResult(value=self._decode(raw), from_cache=True)
        return self._miss_sync(key, ttl, generator)

    def _miss_fail_fast(self, key: str) -> CacheResult[T]:
        raise CacheMissError(key)

    def _miss_cooperative(
        self, key: str, ttl: int, generator: GeneratorFn[T]
    ) -> CacheResult[T]:
        """First caller generates; others wait up to ``cooperative_timeout``."""
        full_key = self._full_key(key)
        with self._lock.acquire(full_key, timeout=self._coop_timeout) as acquired:
            if acquired:
                # Double-check after acquiring lock
                try:
                    return self.get(key)
                except KeyError:
                    pass
                t0 = time.monotonic()
                value = generator()
                self._record_gen_delta(full_key, time.monotonic() - t0)
                self.set(key, value, ttl)
                return CacheResult(value=value, from_cache=False)
            else:
                # Timeout expired: generate directly without caching
                value = generator()
                return CacheResult(value=value, from_cache=False)

    # ------------------------------------------------------------------
    # Hit refresh strategies
    # ------------------------------------------------------------------

    def _handle_hit_refresh(
        self,
        key: str,
        ttl: int,
        generator: GeneratorFn[T] | None,
        policy: HitRefreshPolicy,
        opts: _CallOpts,
    ) -> None:
        if generator is None or policy == HitRefreshPolicy.NONE:
            return

        full_key = self._full_key(key)

        match policy:
            case HitRefreshPolicy.DEFAULT:
                if self._should_refresh(full_key):
                    self._executor.submit(self._bg_refresh, key, ttl, generator)

            case HitRefreshPolicy.AHEAD:
                remaining = self._redis.ttl(full_key)
                threshold = (
                    opts.refresh_ahead_threshold
                    if opts.refresh_ahead_threshold is not None
                    else self._ahead_threshold
                )
                if remaining > 0 and (remaining / ttl) < threshold:
                    if self._should_refresh(full_key):
                        self._executor.submit(self._bg_refresh, key, ttl, generator)

            case HitRefreshPolicy.PROBABILISTIC:
                remaining = self._redis.ttl(full_key)
                if remaining > 0:
                    beta = (
                        opts.probabilistic_beta
                        if opts.probabilistic_beta is not None
                        else self._prob_beta
                    )
                    with self._meta_lock:
                        delta = self._gen_delta.get(full_key, 1.0)
                    # XFetch: refresh if delta * beta * (-ln(rand)) > remaining_ttl
                    if delta * beta * (-math.log(random.random())) > remaining:
                        if self._should_refresh(full_key):
                            self._executor.submit(self._bg_refresh, key, ttl, generator)

            case HitRefreshPolicy.OLDER_THAN:
                remaining = self._redis.ttl(full_key)
                if remaining > 0:
                    age = ttl - remaining
                    threshold = (
                        opts.refresh_older_than
                        if opts.refresh_older_than is not None
                        else self._older_than
                    )
                    if age > threshold:
                        if self._should_refresh(full_key):
                            self._executor.submit(self._bg_refresh, key, ttl, generator)

    # ------------------------------------------------------------------
    # Background workers (run in thread pool)
    # ------------------------------------------------------------------

    def _bg_write(self, key: str, ttl: int, value: T) -> None:
        """Background write for ASYNC miss-fill policy."""
        full_key = self._full_key(key)
        lock = self._lock.try_acquire(full_key)
        if lock is None:
            return  # Another thread is already writing
        try:
            if self._redis.exists(full_key):
                return  # Key appeared while we were waiting
            self.set(key, value, ttl)
        except Exception:
            log.exception("cashcov: background write failed for key %r", key)
        finally:
            lock.release()

    def _bg_refresh(self, key: str, ttl: int, generator: GeneratorFn[T]) -> None:
        """Background refresh for hit-refresh policies and STALE_OR_SYNC."""
        full_key = self._full_key(key)
        lock = self._lock.try_acquire(full_key)
        if lock is None:
            return  # Another thread is already refreshing
        try:
            if not self._should_refresh(full_key):
                return
            t0 = time.monotonic()
            value = generator()
            self._record_gen_delta(full_key, time.monotonic() - t0)
            self.set(key, value, ttl)
        except Exception:
            log.exception("cashcov: background refresh failed for key %r", key)
        finally:
            lock.release()
