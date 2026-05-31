"""High-level Python interface to the cashcov cache library.

Example::

    import json
    from cashcov import CacheHandler
    from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

    handler = CacheHandler(
        redis_addr="localhost:6379",
        prefix="myapp",
        ttl=300,
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.2,
    )

    def generate(key: str) -> str:
        return json.dumps({"result": f"computed for {key}"})

    raw = handler.get_or_refresh("my-key", generate)
    data = json.loads(raw)
    handler.close()

    # Per-call policy override:
    raw = handler.get_or_refresh(
        "other-key",
        generate,
        miss_fill_policy=MissFillPolicy.FAIL_FAST,
    )

    # Context manager:
    with CacheHandler(redis_addr="localhost:6379", prefix="myapp", ttl=300) as h:
        raw = h.get_or_refresh("my-key", generate)
"""

from __future__ import annotations

import ctypes
import json
from typing import Callable

from cashcov._bindings import GENERATOR_FN, _lib
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy

# Sentinel: passed to the shim to mean "no per-call override, use handler default".
_NO_OVERRIDE = -1


class CacheError(RuntimeError):
    """Raised when the underlying cache operation fails."""


class CacheHandler:
    """A handle to a cashcov cache handler backed by Redis.

    All values are exchanged as raw JSON strings.  The caller is responsible
    for serialising and deserialising richer Python types.

    Handler-level policy arguments set the *default* behaviour for every call.
    They can be overridden per-call via :meth:`get_or_refresh`.

    Args:
        redis_addr:              Redis server address (``"host:port"``).
        prefix:                  Key prefix applied to every cache entry.
        ttl:                     Default TTL in seconds.
        miss_fill_policy:        What to do on a cache miss.
        hit_refresh_policy:      When to trigger a background refresh on a hit.
        error_policy:            How to surface generator errors.
        stale_ttl:               Seconds to keep stale data for STALE_OR_SYNC policy.
        refresh_cooldown:        Minimum seconds between background refreshes (hit path).
        dedup_window:            Seconds in which a second miss for the same key skips
                                 the generator and retries Redis instead (ASYNC stampede guard).
        cooperative_timeout:     Seconds other callers wait when COOPERATIVE is active.
        refresh_ahead_threshold: Fraction of TTL remaining that triggers AHEAD refresh
                                 (e.g. ``0.2`` = refresh when 20 % TTL remains).
        refresh_older_than:      Seconds of age that triggers OLDER_THAN refresh.
        probabilistic_beta:      Sensitivity for PROBABILISTIC refresh (default 1.0).

    Raises:
        CacheError: If the underlying handler cannot be created.
    """

    def __init__(
        self,
        *,
        redis_addr: str = "localhost:6379",
        prefix: str = "",
        ttl: int = 300,
        miss_fill_policy: MissFillPolicy = MissFillPolicy.DEFAULT,
        hit_refresh_policy: HitRefreshPolicy = HitRefreshPolicy.DEFAULT,
        error_policy: ErrorPolicy = ErrorPolicy.SURFACE,
        stale_ttl: int = 0,
        refresh_cooldown: int = 0,
        dedup_window: int = 0,
        cooperative_timeout: int = 0,
        refresh_ahead_threshold: float = 0.0,
        refresh_older_than: int = 0,
        probabilistic_beta: float = 0.0,
    ) -> None:
        config: dict = {
            "prefix": prefix,
            "ttl_secs": ttl,
            "miss_fill_policy": int(miss_fill_policy),
            "hit_refresh_policy": int(hit_refresh_policy),
            "error_policy": int(error_policy),
        }
        if stale_ttl > 0:
            config["stale_ttl_secs"] = stale_ttl
        if refresh_cooldown > 0:
            config["refresh_cooldown_secs"] = refresh_cooldown
        if dedup_window > 0:
            config["dedup_window_secs"] = dedup_window
        if cooperative_timeout > 0:
            config["cooperative_timeout_secs"] = cooperative_timeout
        if refresh_ahead_threshold > 0.0:
            config["refresh_ahead_threshold"] = refresh_ahead_threshold
        if refresh_older_than > 0:
            config["refresh_older_than_secs"] = refresh_older_than
        if probabilistic_beta > 0.0:
            config["probabilistic_beta"] = probabilistic_beta

        config_json = json.dumps(config).encode()
        handle = _lib.CashCov_NewHandler(redis_addr.encode(), config_json)
        if handle < 0:
            raise CacheError(f"Failed to create cache handler (redis_addr={redis_addr!r})")
        self._handle = handle
        self._closed = False

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def get_or_refresh(
        self,
        key: str,
        generator: Callable[[str], str],
        *,
        miss_fill_policy: MissFillPolicy | None = None,
        hit_refresh_policy: HitRefreshPolicy | None = None,
        error_policy: ErrorPolicy | None = None,
    ) -> str:
        """Return the cached JSON string for *key*, generating it if absent.

        How the generator is invoked
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        *generator* is a plain Python callable ``(key: str) -> str``.  On a
        cache miss the Go library calls it synchronously (via a C function
        pointer) to produce a fresh JSON-encoded value, which is then written
        to Redis according to the active ``miss_fill_policy``.

        The generator is **not** called on a cache hit.  If the active policy
        is :attr:`MissFillPolicy.ASYNC`, the value is returned to the caller
        immediately and the Redis write happens in the background — subsequent
        callers will find the key in the cache.

        Return ``None`` or raise any exception inside the generator to signal a
        generation failure; ``get_or_refresh`` will raise :exc:`CacheError`.

        Per-call policy overrides
        ~~~~~~~~~~~~~~~~~~~~~~~~~
        Pass ``miss_fill_policy``, ``hit_refresh_policy``, or ``error_policy``
        to override the handler-level defaults for this single call only.

        Args:
            key:                Cache key.
            generator:          ``(key: str) -> str`` — returns a JSON string.
            miss_fill_policy:   Per-call override; ``None`` = use handler default.
            hit_refresh_policy: Per-call override; ``None`` = use handler default.
            error_policy:       Per-call override; ``None`` = use handler default.

        Returns:
            The cached or freshly generated JSON string.

        Raises:
            CacheError: If the handler is closed or the operation fails.
        """
        self._check_open()

        def _c_generator(c_key: bytes) -> bytes | None:
            try:
                result = generator(c_key.decode())
                if result is None:
                    return None
                return result.encode() if isinstance(result, str) else result
            except Exception:  # noqa: BLE001
                return None

        # c_gen must stay alive for the entire duration of the C call.
        c_gen = GENERATOR_FN(_c_generator)

        result_ptr = _lib.CashCov_GetOrRefresh(
            self._handle,
            key.encode(),
            c_gen,
            ctypes.c_int(_NO_OVERRIDE if miss_fill_policy is None else int(miss_fill_policy)),
            ctypes.c_int(_NO_OVERRIDE if hit_refresh_policy is None else int(hit_refresh_policy)),
            ctypes.c_int(_NO_OVERRIDE if error_policy is None else int(error_policy)),
        )
        if result_ptr is None:
            raise CacheError(f"get_or_refresh failed for key {key!r}")

        value = result_ptr.decode()
        _lib.CashCov_Free(result_ptr)
        return value

    def set(self, key: str, value: str, ttl: int = 0) -> None:
        """Write a JSON string directly to the cache.

        Args:
            key:   Cache key.
            value: JSON-encoded string to store.
            ttl:   TTL in seconds.  Pass ``0`` to use the handler default.

        Raises:
            CacheError: If the handler is closed or the write fails.
        """
        self._check_open()
        rc = _lib.CashCov_Set(
            self._handle,
            key.encode(),
            value.encode(),
            ctypes.c_int(ttl),
        )
        if rc != 0:
            raise CacheError(f"set failed for key {key!r}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release all resources. Safe to call more than once."""
        if not self._closed:
            _lib.CashCov_DestroyHandler(self._handle)
            self._closed = True

    def __enter__(self) -> CacheHandler:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise CacheError("CacheHandler is closed")
