"""cashcov — Pure-Python Redis cache with background refresh and stampede protection.

Quick start::

    import redis
    from cashcov import CacheHandler
    from cashcov.policies import MissFillPolicy, HitRefreshPolicy

    rdb = redis.Redis(host="localhost", port=6379, decode_responses=False)

    with CacheHandler[dict](rdb, prefix="myapp", ttl=300) as cache:
        result = cache.get_or_refresh("user:42", generator=lambda: fetch_user(42))

Async (FastAPI / asyncio)::

    import redis.asyncio as aioredis
    from cashcov import AsyncCacheHandler

    rdb = aioredis.Redis(host="localhost", port=6379, decode_responses=False)

    async with AsyncCacheHandler[dict](rdb, prefix="myapp", ttl=300) as cache:
        result = await cache.get_or_refresh("user:42", generator=fetch_user_async)

FastAPI integration::

    from cashcov.ext.fastapi import CacheManager

    cache_manager = CacheManager(redis_url="redis://localhost:6379", prefix="myapp", ttl=300)

Testing (no Redis required)::

    from cashcov.testing import MockCacheHandler, AsyncMockCacheHandler

    mock = MockCacheHandler[dict]()
    mock.seed("user:42", {"id": 42, "name": "Alice"})
"""

from cashcov._async_handler import AsyncCacheHandler
from cashcov._config import handler_kwargs_from_env
from cashcov._handler import CacheHandler
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy
from cashcov.types import CacheMissError, CacheResult

__all__ = [
    "CacheHandler",
    "AsyncCacheHandler",
    "CacheResult",
    "CacheMissError",
    "MissFillPolicy",
    "HitRefreshPolicy",
    "ErrorPolicy",
    "handler_kwargs_from_env",
]
