# cashcov (pure Python)

Pure-Python Redis cache with background refresh, stampede protection, and FastAPI integration.
No CGo, no shared libraries — install with `pip` or `uv add cashcov`.

## Features

- **All six miss-fill policies**: SYNC, ASYNC, STALE_OR_SYNC, FAIL_FAST, COOPERATIVE
- **Four hit-refresh policies**: DEFAULT, AHEAD (refresh-ahead), PROBABILISTIC (XFetch), OLDER_THAN
- **Three error policies**: SURFACE, ZERO_VALUE
- **Async-native**: `AsyncCacheHandler` built on `redis.asyncio`
- **FastAPI integration**: lifespan manager + dependency injection
- **Testing mocks**: `MockCacheHandler` / `AsyncMockCacheHandler` — no Redis needed
- **`@cached` decorator**: function-level caching with custom key functions
- **Fully typed**: `py.typed` marker, `Generic[T]` support

## Installation

### From PyPI

Once published, installation will be:

```bash
# Runtime only
uv add cashcov
pip install cashcov

# With FastAPI extras
uv add "cashcov[fastapi]"
pip install "cashcov[fastapi]"
```

### From GitHub

```bash
# Runtime only
uv add "cashcov @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"
pip install "cashcov @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"

# With FastAPI extras
uv add "cashcov[fastapi] @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"
pip install "cashcov[fastapi] @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"
```

### Development (editable install with all extras)

```bash
git clone https://github.com/Hossein-Roshandel/cashcov.git
cd cashcov/cashcov-py
uv sync --extra dev
uv run pytest          # 90 tests, ~7 s
```

## Policies

cashcov has three independent policy axes. Each can be set at handler level (the default for every call) and overridden per call.

### 1 — `MissFillPolicy` — what happens on a cache miss

| Policy | Behaviour |
|---|---|
| `SYNC` *(default)* | Acquire a per-key lock → double-check Redis → call generator → write → return. Prevents stampede. |
| `ASYNC` | Return immediately with `from_cache=False`; write to Redis in the background. Lowest miss latency. |
| `STALE_OR_SYNC` | Serve stale (expired) data immediately + trigger background refresh. Falls back to `SYNC` when no stale entry exists. Requires `stale_ttl > 0`. |
| `FAIL_FAST` | Raise `CacheMissError` without calling the generator. Use for explicit fallback / circuit-breaker patterns. |
| `COOPERATIVE` | First concurrent caller generates; all others for the same key block until it finishes. |

### 2 — `HitRefreshPolicy` — proactive refresh when the key is in the cache

| Policy | Behaviour |
|---|---|
| `DEFAULT` | Background refresh on every hit, gated by `refresh_cooldown`. |
| `AHEAD` | Refresh when remaining TTL < `refresh_ahead_threshold × ttl` (e.g. 20 % left). |
| `PROBABILISTIC` | XFetch algorithm: refresh probability rises as the entry ages. Tune with `probabilistic_beta`. |
| `OLDER_THAN` | Refresh when entry age > `refresh_older_than` seconds. |
| `NONE` | No background refresh. |

### 3 — `ErrorPolicy` — what happens when the generator raises

| Policy | Behaviour |
|---|---|
| `SURFACE` *(default)* | Re-raise the exception to the caller. |
| `ZERO_VALUE` | Suppress the exception and return `CacheResult(value=None, from_cache=False)`. Does **not** suppress `CacheMissError`. |

---

## Quick start

```python
import redis
from cashcov import CacheHandler
from cashcov.policies import MissFillPolicy, HitRefreshPolicy

rdb = redis.Redis(host="localhost", port=6379, decode_responses=False)

with CacheHandler[dict](rdb, prefix="myapp", ttl=300) as cache:
    result = cache.get_or_refresh("user:42", generator=lambda: fetch_user(42))
    print(result.value, result.from_cache)
```

---

## FastAPI — per-endpoint policies

The recommended pattern is a **single shared handler** injected via `Depends()`, with each endpoint specifying its own policy overrides directly in `get_or_refresh()`. This keeps all caching behaviour explicit at the call site without needing multiple handlers.

```python
from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import Depends, FastAPI
from cashcov import AsyncCacheHandler
from cashcov.ext.fastapi import CacheManager
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

cache_manager = CacheManager(
    redis_url="redis://localhost:6379",
    prefix="myapp",
    ttl=300,
    # Handler-level defaults — apply when not overridden per-call.
    miss_fill_policy=MissFillPolicy.SYNC,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,
)
cache_dep = cache_manager.get_dependency()
CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with cache_manager.lifespan(app):
        yield

app = FastAPI(lifespan=lifespan)


# ── High-traffic product catalogue ─────────────────────────────────────────
# Serve stale data instantly while a background refresh runs.
# Falls back to SYNC when no stale entry exists (e.g. first request).
@app.get("/products/{product_id}")
async def get_product(product_id: str, cache: CacheDep):
    result = await cache.get_or_refresh(
        f"product:{product_id}",
        generator=lambda: db.fetch_product(product_id),
        miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
        stale_ttl=600,          # keep a stale shadow key for 10 min
        hit_refresh_policy=HitRefreshPolicy.AHEAD,
        refresh_ahead_threshold=0.25,
    )
    return result.value


# ── User sessions — must always be fresh ───────────────────────────────────
# SYNC fill + no background refresh ensures callers never see stale state.
@app.get("/sessions/{session_id}")
async def get_session(session_id: str, cache: CacheDep):
    result = await cache.get_or_refresh(
        f"session:{session_id}",
        generator=lambda: auth.load_session(session_id),
        miss_fill_policy=MissFillPolicy.SYNC,
        hit_refresh_policy=HitRefreshPolicy.NONE,
        ttl=900,                # 15-minute session TTL
    )
    return result.value


# ── Expensive reports — fire-and-forget generation ─────────────────────────
# Return immediately on a miss so the HTTP response is instant;
# tolerate a None value while the background task runs.
@app.get("/reports/{report_id}")
async def get_report(report_id: str, cache: CacheDep):
    result = await cache.get_or_refresh(
        f"report:{report_id}",
        generator=lambda: reporting.build_report(report_id),
        miss_fill_policy=MissFillPolicy.ASYNC,
        hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
        refresh_older_than=3600,  # rebuild hourly
        error_policy=ErrorPolicy.ZERO_VALUE,  # don't 500 if report fails
        ttl=7200,
    )
    return result.value or {"status": "generating"}


# ── Configuration — fail fast if cache is cold ─────────────────────────────
# Config must be pre-populated; raise immediately rather than calling the DB.
@app.get("/config/{key}")
async def get_config(key: str, cache: CacheDep):
    from cashcov import CacheMissError
    try:
        result = await cache.get_or_refresh(
            f"config:{key}",
            miss_fill_policy=MissFillPolicy.FAIL_FAST,
            hit_refresh_policy=HitRefreshPolicy.NONE,
        )
        return result.value
    except CacheMissError:
        raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
```

### Alternative: multiple named handlers

When a group of endpoints shares the same policy profile, a dedicated handler
with those defaults pre-set reads more cleanly than repeating overrides.

```python
# Short-lived, always-fresh handler for auth/session data
session_cache_manager = CacheManager(
    redis_url=REDIS_URL, prefix="session", ttl=900,
    miss_fill_policy=MissFillPolicy.SYNC,
    hit_refresh_policy=HitRefreshPolicy.NONE,
)
session_dep = session_cache_manager.get_dependency()
SessionCacheDep = Annotated[AsyncCacheHandler, Depends(session_dep)]

# Long-lived, stale-tolerated handler for catalogue data
catalogue_cache_manager = CacheManager(
    redis_url=REDIS_URL, prefix="catalogue", ttl=3600,
    miss_fill_policy=MissFillPolicy.STALE_OR_SYNC, stale_ttl=7200,
    hit_refresh_policy=HitRefreshPolicy.AHEAD, refresh_ahead_threshold=0.2,
)
catalogue_dep = catalogue_cache_manager.get_dependency()
CatalogueCacheDep = Annotated[AsyncCacheHandler, Depends(catalogue_dep)]

# Wire both managers into the lifespan:
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with session_cache_manager.lifespan(app):
        async with catalogue_cache_manager.lifespan(app):
            yield
```

---

## Testing (no Redis required)

```python
from cashcov.testing import AsyncMockCacheHandler

async def test_get_product(client):
    mock = AsyncMockCacheHandler()
    mock.seed("product:p1", {"id": "p1", "name": "Widget", "price": 9.99})
    app.dependency_overrides[cache_dep] = lambda: mock

    resp = await client.get("/products/p1")
    assert resp.status_code == 200
    assert "product:p1" in mock.get_or_refresh_calls

    app.dependency_overrides.clear()


# Simulate a cache miss (force generator to be called)
async def test_get_product_miss(client):
    mock = AsyncMockCacheHandler()
    mock.force_miss("product:p1")
    app.dependency_overrides[cache_dep] = lambda: mock
    ...


# Simulate a backend error
async def test_report_generation_error(client):
    mock = AsyncMockCacheHandler()
    mock.inject_error("report:r1", RuntimeError("DB timeout"))
    app.dependency_overrides[cache_dep] = lambda: mock
    ...
```
