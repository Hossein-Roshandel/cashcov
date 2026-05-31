"""cashcov — Python bindings for the cashcov Redis cache library.

Usage::

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
        return json.dumps({"data": f"fresh value for {key}"})

    value = handler.get_or_refresh("my-key", generate)
    handler.close()

All values are exchanged as JSON strings.  Serialisation and deserialisation
of richer types is the caller's responsibility.
"""

from cashcov.client import CacheError, CacheHandler
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy

__all__ = [
    "CacheHandler",
    "CacheError",
    "MissFillPolicy",
    "HitRefreshPolicy",
    "ErrorPolicy",
]
