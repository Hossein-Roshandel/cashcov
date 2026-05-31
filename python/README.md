# cashcov Python bindings

Python package for the [cashcov](https://github.com/Hossein-Roshandel/cashcov) Redis cache library, built on a CGo shared library.

All three policy axes (miss-fill, hit-refresh, error) are fully exposed. The
shared library (`libcashcov.so` / `libcashcov.dylib`) is compiled from Go source
automatically during `pip install`.

## Requirements

- Python 3.9+
- Go 1.21+ on `PATH` (only needed at install time)
- A C compiler (`gcc` / `clang`) for CGo

## Install

```bash
# From PyPI (once published):
pip install cashcov

# Directly from GitHub (no clone needed — Go is compiled on your machine):
pip install git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=python

# From a local clone of the repository:
pip install ./python/          # standard install (compiles libcashcov.so)
pip install -e ./python/       # editable install

# Or with uv:
uv pip install git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=python
```

The `hatch_build.py` build hook runs `go build -buildmode=c-shared` automatically, so no manual compilation step is needed.

## Quick start

```python
import json
from cashcov import CacheHandler

def generate(key: str) -> str:
    return json.dumps({"result": f"computed for {key}"})

with CacheHandler(redis_addr="localhost:6379", prefix="myapp", ttl=300) as cache:
    raw = cache.get_or_refresh("my-key", generate)
    data = json.loads(raw)  # {"result": "computed for my-key"}

    # Second call is a cache hit — generator not invoked
    raw2 = cache.get_or_refresh("my-key", generate)
```

## Policy overview

The three axes map directly to the Go library's policy types:

```python
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy
```

### `MissFillPolicy` — what to do on a cache miss

| Value | Description |
|-------|-------------|
| `DEFAULT` (= `SYNC`) | Alias for `SYNC`; zero value |
| `SYNC` | Block caller, generate, write, return |
| `ASYNC` | Generate, return immediately, write in background |
| `STALE_OR_SYNC` | Return stale data while refreshing in background |
| `FAIL_FAST` | Return `None` immediately without calling the generator |
| `COOPERATIVE` | Wait up to `cooperative_timeout` for another goroutine's result |

### `HitRefreshPolicy` — when to trigger background refresh on a hit

| Value | Description |
|-------|-------------|
| `DEFAULT` | Refresh on every hit, gated by `refresh_cooldown` |
| `AHEAD` | Refresh when remaining TTL drops below `refresh_ahead_threshold` fraction |
| `PROBABILISTIC` | XFetch: refresh probability grows with entry age (`probabilistic_beta`) |
| `OLDER_THAN` | Refresh when entry age exceeds `refresh_older_than` seconds |
| `NONE` | Never refresh proactively |

### `ErrorPolicy` — how to surface generator errors

| Value | Description |
|-------|-------------|
| `SURFACE` (default) | Return the error to the caller |
| `ZERO_VALUE` | Suppress error; return `None` (never suppresses `ErrCacheMiss`) |

## API reference

### `CacheHandler`

```python
CacheHandler(
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
)
```

All `int` time parameters are in **seconds**.

#### Handler-level parameter details

| Parameter | Policy | Description |
|-----------|--------|-------------|
| `stale_ttl` | `STALE_OR_SYNC` | How long to keep a stale copy (seconds) |
| `refresh_cooldown` | `DEFAULT` | Minimum gap between background refreshes |
| `dedup_window` | any miss | Suppress duplicate generation for this many seconds after a write |
| `cooperative_timeout` | `COOPERATIVE` | How long waiting callers block before generating directly |
| `refresh_ahead_threshold` | `AHEAD` | Fraction of TTL remaining that triggers refresh (e.g. `0.2` = 20 %) |
| `refresh_older_than` | `OLDER_THAN` | Entry age in seconds that triggers refresh |
| `probabilistic_beta` | `PROBABILISTIC` | XFetch sensitivity (default `1.0`) |

### `handler.get_or_refresh(key, generator, *, miss_fill_policy=None, hit_refresh_policy=None, error_policy=None) -> str | None`

Return the cached JSON string for `key`. Calls `generator(key)` on a miss.

Per-call `miss_fill_policy`, `hit_refresh_policy`, and `error_policy` override the handler defaults for this call only. Pass `None` (default) to use the handler default.

Returns `None` when `MissFillPolicy.FAIL_FAST` is active and the key is absent, or when `ErrorPolicy.ZERO_VALUE` suppresses an error.

### `handler.set(key, value, ttl=0)`

Write a raw string (typically JSON) directly to the cache, bypassing the generator.

### `handler.close()`

Release all resources. Prefer the context manager (`with` statement).

## Examples

All examples are in [`examples/`](examples/) and require Redis on `localhost:6379`.

| File | What it shows |
|------|---------------|
| [`basic.py`](examples/basic.py) | Core `get_or_refresh` pattern — generator called once, cache hit on repeat |
| [`typed_values.py`](examples/typed_values.py) | Dataclass serialisation via a thin `TypedCache` JSON wrapper |
| [`multiple_handlers.py`](examples/multiple_handlers.py) | Separate handlers per domain (sessions, config, rate-limits) with different TTLs |
| [`error_handling.py`](examples/error_handling.py) | Handling `CacheError`, flaky generators, and fallback values |
| [`policies.py`](examples/policies.py) | Full demonstration of all three policy axes and per-call overrides |

```bash
# Install (any of the methods above), then run any example
pip install -e ./python/
python python/examples/basic.py
python python/examples/policies.py
```

## Policy examples

### MissFillAsync with deduplication

```python
from cashcov import CacheHandler
from cashcov.policies import MissFillPolicy

cache = CacheHandler(
    redis_addr="localhost:6379",
    prefix="products",
    ttl=300,
    miss_fill_policy=MissFillPolicy.ASYNC,
    dedup_window=30,  # suppress duplicate generator calls for 30s
)
```

### HitRefreshAhead

```python
from cashcov.policies import HitRefreshPolicy

cache = CacheHandler(
    redis_addr="localhost:6379",
    prefix="prices",
    ttl=60,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,  # refresh when < 20% TTL remaining
)
```

### Per-call circuit-breaker

```python
from cashcov.policies import MissFillPolicy

result = cache.get_or_refresh(
    "my-key",
    generate,
    miss_fill_policy=MissFillPolicy.FAIL_FAST,  # return None if not cached
)
if result is None:
    result = fallback_value
```

### Cooperative stampede protection

```python
from cashcov.policies import MissFillPolicy

cache = CacheHandler(
    redis_addr="localhost:6379",
    prefix="reports",
    ttl=600,
    miss_fill_policy=MissFillPolicy.COOPERATIVE,
    cooperative_timeout=5,  # wait up to 5s for another goroutine
)
```
