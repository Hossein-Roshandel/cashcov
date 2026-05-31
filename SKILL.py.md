---
name: cashcov-python
description: >
  Domain knowledge for using, extending, and debugging the cashcov Python wrapper.
  Use when: writing or reviewing Python code that imports cashcov; adding cache policies;
  debugging CacheError or segfaults in the ctypes bridge; working on client.py,
  _bindings.py, or the Python tests; updating examples or README.
---

# cashcov — Python Skill

## What cashcov is

cashcov is a **Redis-backed generic cache library** written in Go. The Python
package wraps it via a compiled C shared library (`libcashcov.so`) loaded at
runtime by `ctypes`. All values are exchanged as **JSON strings** — the Python
caller serialises before writing and deserialises after reading.

```
Python  →  ctypes  →  libcashcov.so (CGo shim)  →  Go Handler[string]  →  Redis
```

The Python package lives in `python/cashcov/` and has three public modules:

| Module | Purpose |
|---|---|
| `cashcov` (`__init__.py`) | Public API: re-exports `CacheHandler`, `CacheError`, and the three policy enums |
| `cashcov.client` | `CacheHandler` class — the only interface callers should use |
| `cashcov.policies` | `MissFillPolicy`, `HitRefreshPolicy`, `ErrorPolicy` IntEnums |
| `cashcov._bindings` | Private — ctypes declarations; never import directly |

---

## Three independent policy axes

Every handler and every call is governed by three independent axes.

### 1. MissFillPolicy — what to do on a cache miss

| Value | Behaviour | Required companion params |
|---|---|---|
| `DEFAULT` | Alias for `SYNC` | — |
| `SYNC` | Lock → double-check → generate → write → return. Prevents stampede. | — |
| `ASYNC` | Generate → return immediately; write to Redis in background. Lowest latency; no stampede protection on first wave. | `dedup_window` to suppress duplicates |
| `STALE_OR_SYNC` | Return stale data immediately + background rewrite; fall back to SYNC when no stale key exists. | `stale_ttl > 0`, `generator=` at construction |
| `FAIL_FAST` | Return `None` (or raise `CacheError`) without calling the generator. | — |
| `COOPERATIVE` | First caller locks and generates; others block up to `cooperative_timeout`, then fall back to direct generation. | `cooperative_timeout > 0` |

### 2. HitRefreshPolicy — proactive background refresh on a hit

| Value | Behaviour | Required companion params |
|---|---|---|
| `DEFAULT` | Background refresh on every hit, gated by `refresh_cooldown`. | `generator=` at construction |
| `AHEAD` | Refresh when remaining TTL ≤ `refresh_ahead_threshold × original TTL`. | `generator=`, `refresh_ahead_threshold > 0` |
| `PROBABILISTIC` | XFetch algorithm — refresh probability rises continuously as entry ages. | `generator=`, `probabilistic_beta` (default 1.0) |
| `OLDER_THAN` | Refresh when entry age (originalTTL − remaining TTL) ≥ `refresh_older_than`. | `generator=`, `refresh_older_than > 0` |
| `NONE` | No background refresh. | — |

### 3. ErrorPolicy — how generator errors surface

| Value | Behaviour |
|---|---|
| `SURFACE` | Raise `CacheError`. Default. |
| `ZERO_VALUE` | Return `""` (empty string), no exception. `ErrCacheMiss` from `FAIL_FAST` is never suppressed. |

---

## The two-generator design (critical)

Background goroutines (`HitRefreshAhead`, `HitRefreshDefault`, etc.) run on
Go's scheduler and **may call the generator after `get_or_refresh` has
returned**. A per-call `ctypes.CFUNCTYPE` object created inside
`get_or_refresh` would be garbage-collected by then → segfault.

The fix is a **two-lifetime** design:

- **Handler-level `generator=`** — passed at `CacheHandler` construction,
  stored as `self._bg_c_gen` for the lifetime of the handler. Go background
  goroutines use this pointer. It is registered with `CashCov_SetGenerator`.
- **Per-call `generator` argument** — used **only** for the synchronous
  miss-fill path within the `get_or_refresh` call. Never outlives the call
  when `hasBgGen` is `False`; when `hasBgGen` is `True` the shim uses the
  handler-level generator for all paths and the per-call argument is ignored.

```
handler-level generator=  →  registered in Go bgGenerators map  →  background goroutines
per-call generator arg    →  stack-local CFUNCTYPE               →  synchronous miss-fill only
```

**Rule**: Any `HitRefreshPolicy` other than `NONE`, and `MissFillStaleOrSync`,
**require** `generator=` at construction. The Python `CacheHandler.__init__`
enforces this with `ValueError`.

---

## Construction

```python
from cashcov import CacheHandler
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy
import json

def fetch(key: str) -> str:
    row = db.query("SELECT data FROM items WHERE id = %s", key)
    return json.dumps(row)

# Minimal — synchronous miss-fill only, no background refresh
cache = CacheHandler(redis_addr="localhost:6379", prefix="items", ttl=300)

# With background refresh (generator= is required)
cache = CacheHandler(
    redis_addr="localhost:6379",
    prefix="items",
    ttl=300,
    generator=fetch,                          # handler-level, kept alive for goroutines
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,              # refresh when 20% TTL remains
    refresh_cooldown=30,
)

# Always use as a context manager in production
with CacheHandler(redis_addr="localhost:6379", prefix="items", ttl=300) as cache:
    value = cache.get_or_refresh("item:42", fetch)
    data = json.loads(value)
```

---

## get_or_refresh

```python
value: str = cache.get_or_refresh(
    key,          # str cache key
    generator,    # (key: str) -> str | None  — must return a JSON string or raise
    *,
    miss_fill_policy=None,    # per-call override; None = handler default
    hit_refresh_policy=None,  # per-call override
    error_policy=None,        # per-call override
)
```

- Returns the cached or freshly-generated **JSON string**.
- Raises `CacheError` on failure (unless `ErrorPolicy.ZERO_VALUE` is active).
- Returns `None` when `FAIL_FAST` is active and the key is absent.
- When a handler-level `generator=` is registered, the `generator` argument is
  used for the initial miss-fill documentation only — the shim always invokes
  the registered generator for all paths including goroutines. Pass the same
  function to both for clarity.

### Generator contract

```python
def my_generator(key: str) -> str:
    # Must return a valid JSON string
    return json.dumps({"id": key, "value": "..."})
    # Returning None or raising → generation failure
    # CacheError is raised unless ErrorPolicy.ZERO_VALUE is active
```

---

## set() — direct write

```python
cache.set("key", json.dumps({"x": 1}))          # handler default TTL
cache.set("key", json.dumps({"x": 1}), ttl=60)  # explicit TTL in seconds
```

---

## Lifecycle

```python
# Preferred — context manager
with CacheHandler(...) as cache:
    ...

# Manual
cache = CacheHandler(...)
try:
    ...
finally:
    cache.close()   # safe to call multiple times
```

`close()` calls `CashCov_DestroyHandler` in the shim **before** releasing
`self._bg_c_gen`. This ordering ensures no background goroutine calls the
Python callback after it has been freed.

---

## Construction-time validation (guardrails)

`CacheHandler.__init__` raises `ValueError` for common misconfiguration before
any Redis connection is made:

| Misconfiguration | Error message contains |
|---|---|
| `AHEAD` / `PROBABILISTIC` / `OLDER_THAN` without `generator=` | `"generator="` |
| `STALE_OR_SYNC` without `generator=` | `"generator="` |
| `AHEAD` without `refresh_ahead_threshold > 0` | `"refresh_ahead_threshold"` |
| `OLDER_THAN` without `refresh_older_than > 0` | `"refresh_older_than"` |
| `STALE_OR_SYNC` without `stale_ttl > 0` | `"stale_ttl"` |

---

## Policy combinations quick-reference

```python
# Stampede-safe synchronous fill (default, safe for any load)
CacheHandler(redis_addr=..., prefix=..., ttl=60)

# Low-latency async write, reduce duplicate generation
CacheHandler(..., miss_fill_policy=MissFillPolicy.ASYNC, dedup_window=5)

# Stale-while-revalidate
CacheHandler(
    ...,
    miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
    stale_ttl=3600,     # keep stale copy for 1 h
    generator=fetch,    # required
)

# Refresh-ahead (proactive TTL management)
CacheHandler(
    ...,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,
    generator=fetch,    # required
)

# Refresh entries older than N seconds
CacheHandler(
    ...,
    hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
    refresh_older_than=600,   # 10 minutes
    generator=fetch,
)

# Circuit-breaker pattern — never call the generator
CacheHandler(..., miss_fill_policy=MissFillPolicy.FAIL_FAST)

# Suppress errors for non-critical data
CacheHandler(..., error_policy=ErrorPolicy.ZERO_VALUE)

# Per-call override (does not change handler default)
value = cache.get_or_refresh(key, gen, miss_fill_policy=MissFillPolicy.FAIL_FAST)
```

---

## Value protocol (JSON round-trip)

The Go handler is typed `Handler[string]`. It `json.Marshal`s the Go string
before writing to Redis and `json.Unmarshal`s on read. This means:

- `cache.set("k", "hello")` → Redis stores `"hello"` (JSON-encoded string,
  with the enclosing double-quotes).
- `cache.get_or_refresh("k", ...)` → returns `"hello"` (the raw Go string,
  without extra wrapping).
- Generators must return **already-JSON-encoded strings**:
  `return json.dumps({"id": 1})` → Go stores `"{\"id\": 1}"`.
- After reading: `data = json.loads(value)`.

**Do not double-encode.** `json.dumps(json.dumps({"x": 1}))` produces a
double-encoded string that will need two `json.loads` calls to unwrap.

---

## What NOT to do

```python
# WRONG — background policies without handler-level generator
CacheHandler(hit_refresh_policy=HitRefreshPolicy.AHEAD, refresh_ahead_threshold=0.2)
# → ValueError at construction

# WRONG — STALE_OR_SYNC without stale_ttl
CacheHandler(miss_fill_policy=MissFillPolicy.STALE_OR_SYNC)
# → ValueError at construction

# WRONG — returning a plain Python object from the generator
cache.get_or_refresh("k", lambda _: {"id": 1})   # not a string → TypeError in ctypes

# WRONG — storing a raw Python string (not JSON)
cache.set("k", "hello world")    # Go will fail to json.Unmarshal it on the next read
# Correct:
cache.set("k", json.dumps("hello world"))   # → stores "\"hello world\""

# WRONG — keeping a local CFUNCTYPE for background use (pre-fix pattern)
# The ctypes callback will be GC'd after get_or_refresh returns → segfault.
# Always use generator= at construction for background goroutines.

# WRONG — importing from _bindings directly
from cashcov._bindings import _lib   # private; may change

# WRONG — using get_or_refresh after close()
cache.close()
cache.get_or_refresh(...)  # → CacheError: CacheHandler is closed
```

---

## Testing

```bash
# Rebuild the shared library (required after any shim/Go change)
cd /workspace && go build -buildmode=c-shared -o /workspace/python/cashcov/libcashcov.so /workspace/cshim

# Run the full test suite
cd /workspace/python && uv run pytest tests

# Run a single test class
uv run pytest tests/test_client.py::TestHitRefreshLive -v
```

Tests use **testcontainers** (Docker) for a throwaway Redis instance. Set
`CASHCOV_TEST_REDIS_ADDR=localhost:6379` to skip Docker.

Key test classes:

| Class | What it covers |
|---|---|
| `TestMissFillSync` | Lock + double-check + stampede prevention |
| `TestMissFillAsync` | Background write, dedup window |
| `TestMissFillCooperative` | Timeout + fallback |
| `TestMissFillStaleOrSync` | Stale key detection + background rewrite |
| `TestHitRefreshLive` | Goroutines actually fire and update Redis |
| `TestConstructionValidation` | ValueError guardrails at construction |
| `TestHandlerLifecycle` | close(), context manager, closed-handler errors |
| `TestGeneratorEdgeCases` | None-returning generator, ZERO_VALUE combo |

---

## Architecture notes (ctypes bridge)

```python
# _bindings.py — key types
GENERATOR_FN = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)
# Return type is c_void_p (NOT c_char_p) so ctypes does not copy the string
# into a Python object. The Go shim owns the pointer and calls free().

_lib.CashCov_GetOrRefresh.restype = ctypes.c_void_p
# Read result with:  value = ctypes.string_at(result_ptr).decode()
# Then free:         _lib.CashCov_Free(result_ptr)

# Callbacks must return a malloc'd pointer; Go calls free() on it.
ptr = _libc.malloc(len(b) + 1)
ctypes.memmove(ptr, b + b"\x00", len(b) + 1)
return ptr   # Go will free this
```

Three historical bugs (all fixed) that are easy to re-introduce:

1. **`c_char_p` return type on the generator** — ctypes copies the bytes into
   a Python object and loses the original pointer; Go then frees Python-owned
   memory → heap corruption.
2. **`c_char_p` restype on `CashCov_GetOrRefresh`** — same issue; use
   `c_void_p` + `ctypes.string_at()`.
3. **Stack-local `CFUNCTYPE` used by background goroutines** — the function
   pointer is freed by Python GC after `get_or_refresh` returns; the goroutine
   calls it later → segfault. Fixed by the two-lifetime design.
