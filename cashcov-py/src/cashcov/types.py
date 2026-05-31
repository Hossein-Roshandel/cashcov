"""Core types shared across the cashcov package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheResult(Generic[T]):
    """The value returned by :meth:`~cashcov.CacheHandler.get_or_refresh`.

    Attributes:
        value:      The cached or freshly generated value.
        from_cache: ``True`` if the value was served from Redis;
                    ``False`` if the generator was called.
        cached_at:  UTC timestamp captured when the result was assembled.
    """

    value: T
    from_cache: bool
    cached_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class CacheMissError(Exception):
    """Raised when :attr:`~cashcov.policies.MissFillPolicy.FAIL_FAST` is
    active and the requested key is not present in Redis.

    Attributes:
        key: The unprefixed cache key that was not found.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"Cache miss (FAIL_FAST): {key!r}")
        self.key = key


# Type alias for generator callables (sync handler).
# Import in handler modules rather than here to avoid circular imports.
GeneratorFn = Any  # Callable[[], T] — defined per-handler for typing convenience
