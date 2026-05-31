# cashcov-py — AI Agent Skill

Pure-Python Redis cache library with stampede protection, background refresh,
FastAPI integration, and in-process mock objects for unit testing.

---

## Installation

> The package is **not yet on PyPI**. Install directly from GitHub.

### With uv (recommended)

```bash
# Core (sync + async handler)
uv add "cashcov @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"

# With FastAPI extras
uv add "cashcov[fastapi] @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"
```

### With pip

```bash
pip install "cashcov @ git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=cashcov-py"
```

### Dev / contributing (editable install with all extras)

```bash
git clone https://github.com/Hossein-Roshandel/cashcov.git
cd cashcov/cashcov-py
uv sync --extra dev   # installs pytest, fakeredis, httpx, ruff, mypy, …
uv run pytest         # all 73 tests must pass
```

---

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.11 |
| redis-py | ≥ 5.0 |
| fastapi *(optional)* | ≥ 0.110 |

---

## Package layout

```
src/cashcov/
├── __init__.py            # public API re-exports
├── policies.py            # MissFillPolicy, HitRefreshPolicy, ErrorPolicy
├── types.py               # CacheResult[T], CacheMissError
├── _lock.py               # KeyedLock (threading), AsyncKeyedLock (asyncio)
├── _handler.py            # CacheHandler[T]       — sync, ThreadPoolExecutor
├── _async_handler.py      # AsyncCacheHandler[T]  — asyncio.Lock + asyncio.Task
├── testing.py             # MockCacheHandler, AsyncMockCacheHandler
└── ext/
    └── fastapi.py         # CacheManager (lifespan + Depends())
```

---

## Public API

### `CacheHandler[T]` (sync)

```python
from cashcov import CacheHandler
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

import redis

rdb = redis.Redis(host="localhost", port=6379, decode_responses=False)

with CacheHandler[dict](
    rdb,
    prefix="myapp",        # key namespace
    ttl=300,               # default TTL in seconds
    miss_fill_policy=MissFillPolicy.SYNC,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,  # refresh when 20% TTL remains
) as cache:

    result = cache.get_or_refresh(
        "user:42",
        generator=lambda: fetch_user(42),
    )
    print(result.value)       # dict
    print(result.from_cache)  # bool
    print(result.cached_at)   # datetime (UTC)
```

Key methods:

| Method | Description |
|---|---|
| `get(key)` | Returns `CacheResult` or raises `KeyError` on miss |
| `set(key, value, ttl?)` | Write a value directly |
| `delete(key)` | Evict a key |
| `get_or_refresh(key, generator?, **overrides)` | Main entry point; all policy overrides accepted per-call |
| `cached(key_fn, ttl?, **overrides)` | Decorator factory — wraps a function so its return value is cached |
| `close()` / context manager | Shuts down background worker pool |

### `AsyncCacheHandler[T]` (async)

Mirror of `CacheHandler` — all public methods are coroutines; generator must be `async def () -> T`.

```python
import redis.asyncio as aioredis
from cashcov import AsyncCacheHandler

rdb = aioredis.Redis(host="localhost", port=6379, decode_responses=False)

async with AsyncCacheHandler[dict](rdb, prefix="myapp", ttl=300) as cache:
    result = await cache.get_or_refresh("user:42", generator=fetch_user)
```

Use `asyncio.to_thread` to wrap a sync callable inside an async handler:

```python
result = await cache.get_or_refresh(
    "user:42",
    generator=lambda: asyncio.to_thread(sync_fetch_user, 42),
)
```

### `CacheResult[T]`

Frozen dataclass returned by `get_or_refresh`:

```python
@dataclass(frozen=True, slots=True)
class CacheResult(Generic[T]):
    value: T
    from_cache: bool
    cached_at: datetime  # UTC
```

### `CacheMissError`

Raised when `MissFillPolicy.FAIL_FAST` is active and the key is absent:

```python
from cashcov import CacheMissError

try:
    result = cache.get_or_refresh("key", miss_fill_policy=MissFillPolicy.FAIL_FAST)
except CacheMissError as e:
    print(e.key)  # unprefixed key string
```

---

## Policy reference

### MissFillPolicy — what to do on a cache miss

| Value | Behaviour |
|---|---|
| `SYNC` *(default)* | Lock per key → double-check → call generator → write → return. Prevents stampede. |
| `ASYNC` | Return immediately; write to Redis in the background (lowest miss latency). |
| `STALE_OR_SYNC` | Serve stale (expired) data + trigger background refresh; falls back to `SYNC` when no stale entry exists. Requires `stale_ttl > 0`. |
| `FAIL_FAST` | Raise `CacheMissError` — no generator call. Circuit-breaker pattern. |
| `COOPERATIVE` | First caller generates; all others for the same key block until it finishes, then serve the cached result. |

### HitRefreshPolicy — proactive background refresh on a cache hit

| Value | Behaviour |
|---|---|
| `DEFAULT` | Background refresh on every hit, gated by `refresh_cooldown`. |
| `AHEAD` | Refresh when remaining TTL < `refresh_ahead_threshold × ttl`. |
| `PROBABILISTIC` | XFetch algorithm — refresh probability rises continuously as entry ages. Tune with `probabilistic_beta`. |
| `OLDER_THAN` | Refresh when entry age > `refresh_older_than` seconds. |
| `NONE` | No background refresh. Best for deterministic tests. |

### ErrorPolicy — how generator errors are handled

| Value | Behaviour |
|---|---|
| `SURFACE` *(default)* | Re-raise the exception to the caller. |
| `ZERO_VALUE` | Suppress the exception and return `CacheResult(value=None, from_cache=False)`. Does **not** suppress `CacheMissError`. |

---

## `@cached` decorator

```python
@cache.cached(key_fn=lambda uid: f"user:{uid}", ttl=60, hit_refresh_policy=HitRefreshPolicy.NONE)
def get_user(uid: str) -> dict:
    return db.query_user(uid)

# Async variant — same decorator, async function
@async_cache.cached(key_fn=lambda uid: f"user:{uid}")
async def get_user_async(uid: str) -> dict:
    return await db.aquery_user(uid)
```

- `key_fn` receives the same positional and keyword arguments as the decorated function.
- All policy overrides accepted by `get_or_refresh` are also accepted by `cached`.

---

## FastAPI integration

### app.py

```python
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from cashcov import AsyncCacheHandler
from cashcov.ext.fastapi import CacheManager

cache_manager = CacheManager(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),
    prefix="myapp",
    ttl=300,
)

# Must be module-level — identity is used as the key for dependency_overrides.
cache_dep = cache_manager.get_dependency()
CacheDep = Annotated[AsyncCacheHandler, Depends(cache_dep)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with cache_manager.lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/users/{uid}")
async def get_user(uid: str, cache: CacheDep):
    async def generate() -> dict:
        return await db.fetch_user(uid)

    result = await cache.get_or_refresh(f"user:{uid}", generate)
    return {"data": result.value, "from_cache": result.from_cache}
```

### Dependency injection in tests

```python
# test_app.py
import pytest
from httpx import AsyncClient, ASGITransport
from cashcov.testing import AsyncMockCacheHandler
from app import app, cache_dep


@pytest.fixture
async def client():
    mock = AsyncMockCacheHandler[dict]()
    mock.seed("user:42", {"id": "42", "name": "Alice"})
    app.dependency_overrides[cache_dep] = lambda: mock
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, mock
    app.dependency_overrides.clear()


async def test_get_user_hit(client):
    ac, mock = client
    resp = await ac.get("/users/42")
    assert resp.status_code == 200
    assert resp.json()["from_cache"] is True
    assert "user:42" in mock.get_or_refresh_calls
```

**Critical rule**: `cache_dep = cache_manager.get_dependency()` must be assigned **once at module level** — `get_dependency()` returns the same callable object each time, and `dependency_overrides` uses object identity as the lookup key.

---

## Mock objects for unit testing

### `MockCacheHandler[T]` (sync)

```python
from cashcov.testing import MockCacheHandler

mock = (
    MockCacheHandler[str]()
    .seed("product:p1", '{"id":"p1","name":"Widget"}')
    .seed("product:p2", '{"id":"p2","name":"Gadget"}')
)

result = mock.get_or_refresh("product:p1", generator=lambda: "...")
assert result.from_cache is True
assert mock.get_or_refresh_calls == ["product:p1"]
```

### `AsyncMockCacheHandler[T]` (async)

```python
from cashcov.testing import AsyncMockCacheHandler

mock = AsyncMockCacheHandler[str]()
mock.seed("user:1", "Alice")
mock.inject_error("user:boom", RuntimeError("DB exploded"))
mock.force_miss("user:stale")

result = await mock.get_or_refresh("user:1")
assert result.value == "Alice"
```

### Mock API

| Method | Description |
|---|---|
| `.seed(key, value)` | Pre-load a value (fluent) |
| `.inject_error(key, exc)` | Raise `exc` when `key` is requested (fluent) |
| `.force_miss(*keys)` | Always treat these keys as absent, even if seeded (fluent) |
| `.reset()` | Clear store, errors, forced misses, and call history (fluent) |
| `.get_calls` | `list[str]` of keys passed to `get()` |
| `.set_calls` | `list[tuple[str, T]]` of `(key, value)` pairs passed to `set()` |
| `.delete_calls` | `list[str]` of keys passed to `delete()` |
| `.get_or_refresh_calls` | `list[str]` of keys passed to `get_or_refresh()` |

---

## Testing notes

- Tests use **fakeredis** (in-process Redis simulation) — no Docker or live Redis needed.
- All async tests run automatically via `asyncio_mode = "auto"` in `pyproject.toml`.
- `hit_refresh_policy=HitRefreshPolicy.NONE` must be set in tests that assert exact generator call counts; the default policy fires a background refresh on every hit.
- Stampede-protection tests must share **one handler instance** across all concurrent callers — each instance has its own `KeyedLock`.
- Run the full suite: `uv run pytest` (73 tests, ~2 s).

---

## Running the examples

```bash
cd cashcov-py

# Requires Redis on localhost:6379
uv run python examples/basic.py
uv run python examples/async_usage.py

# FastAPI example server
uv run uvicorn examples.fastapi_app:app --reload
```

---

## Linting and type checking

```bash
uv run ruff check src tests         # lint
uv run ruff format src tests        # format
uv run mypy                          # strict type check
```
