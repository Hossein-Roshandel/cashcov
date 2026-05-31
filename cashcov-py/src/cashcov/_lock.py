"""Per-key locking primitives for sync and async cache handlers.

Both :class:`KeyedLock` and :class:`AsyncKeyedLock` maintain one lock per
cache key so that concurrent operations on *different* keys never block each
other, while concurrent operations on the *same* key are serialised.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator


class KeyedLock:
    """Thread-safe per-key locking for the synchronous :class:`~cashcov.CacheHandler`.

    A single ``threading.Lock`` is used only to protect the key→lock mapping
    itself; it is released immediately after the per-key lock is retrieved,
    so contention on the meta-lock is negligible.
    """

    def __init__(self) -> None:
        self._meta = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _get(self, key: str) -> threading.Lock:
        with self._meta:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    @contextmanager
    def acquire(self, key: str, timeout: float = -1) -> Iterator[bool]:
        """Context manager that acquires the per-key lock.

        Args:
            key:     The cache key to lock.
            timeout: Seconds to wait; ``-1`` (default) blocks indefinitely;
                     ``0`` is non-blocking.

        Yields:
            ``True`` if the lock was acquired; ``False`` on timeout.
        """
        lock = self._get(key)
        acquired = lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    def try_acquire(self, key: str) -> threading.Lock | None:
        """Non-blocking acquire.

        Returns:
            The acquired lock (caller must call ``.release()``), or ``None``
            if the lock is already held.
        """
        lock = self._get(key)
        if lock.acquire(blocking=False):
            return lock
        return None


class AsyncKeyedLock:
    """Per-key locking for the asynchronous :class:`~cashcov.AsyncCacheHandler`.

    Uses one :class:`asyncio.Lock` per key.  The meta-lock is created lazily
    on first use (inside a running event loop) so the object is safe to
    instantiate at module import time.
    """

    def __init__(self) -> None:
        self._meta: asyncio.Lock | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get(self, key: str) -> asyncio.Lock:
        if self._meta is None:
            self._meta = asyncio.Lock()
        async with self._meta:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    @asynccontextmanager
    async def acquire(
        self, key: str, timeout: float | None = None
    ) -> AsyncIterator[bool]:
        """Async context manager that acquires the per-key lock.

        Args:
            key:     The cache key to lock.
            timeout: Seconds to wait; ``None`` (default) blocks indefinitely.

        Yields:
            ``True`` if the lock was acquired; ``False`` on timeout.
        """
        lock = await self._get(key)
        acquired: bool
        if timeout is not None:
            try:
                await asyncio.wait_for(asyncio.shield(lock.acquire()), timeout=timeout)
                acquired = True
            except asyncio.TimeoutError:
                acquired = False
        else:
            await lock.acquire()
            acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    async def try_acquire(self, key: str) -> asyncio.Lock | None:
        """Non-blocking acquire.

        Safe in asyncio: there is no ``await`` (no context switch) between the
        ``locked()`` check and the ``acquire()`` fast path, so this is
        race-free in a single-threaded event loop.

        Returns:
            The acquired lock (caller must call ``.release()``), or ``None``
            if the lock is already held.
        """
        lock = await self._get(key)
        if lock.locked():
            return None
        # CPython asyncio.Lock.acquire() acquires synchronously when the lock
        # is free — no context switch occurs before _locked is set to True.
        await lock.acquire()
        return lock
