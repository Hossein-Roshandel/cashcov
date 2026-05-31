---
name: cashcov-go
description: >
  Domain knowledge for using, extending, and debugging the cashcov Go library.
  Use when: writing Go code that imports cashcov; adding cache handlers; choosing
  or combining MissFillPolicy / HitRefreshPolicy / ErrorPolicy; debugging
  background refresh or stampede issues; working on cache.go, helper.go,
  policies.go, types.go, lock.go, or the CGo shim in cshim/shim.go.
---

# cashcov — Go Skill

## What cashcov is

cashcov is a **type-safe, Redis-backed generic cache library** for Go.
Its central abstraction is `Handler[T]`, a strongly-typed wrapper around a
Redis client that enforces three independent behavioural axes (miss-fill,
hit-refresh, error) without requiring callers to re-implement caching patterns.

```
Caller → Handler[T].GetOrRefresh(ctx, key, generator, ...CallOption)
                        ↓                   ↓
                  Redis lookup         Generator[T] func(ctx) (T, error)
                        ↓
                  Result[T]{Value, FromCache, CachedAt}
```

Values are stored as `json.Marshal(v)` bytes and decoded with `json.Unmarshal`
on read, so `T` must be JSON-serialisable.

---

## Package layout

| File | Responsibility |
|---|---|
| `cache.go` | `Handler[T]`, `New[T]`, `GetOrRefresh`, `Set`, `Get`, all `With*` option constructors |
| `policies.go` | `MissFillPolicy`, `HitRefreshPolicy`, `ErrorPolicy` iota constants |
| `types.go` | `Result[T]`, `Generator[T]`, `Option`, `CallOption`, `callOpts`, `ErrCacheMiss` |
| `helper.go` | Internal miss/hit helpers: `missSyncWriteThenReturn`, `missReturnThenAsyncWrite`, `spawnBackgroundRefresh`, `spawnStaleRefresh`, etc. |
| `lock.go` | `KeyedMutex` — per-key in-process lock used for stampede prevention |
| `config.go` | `handlerConfig` struct, `loadHandlerConfig` |
| `cshim/shim.go` | CGo shared library exposing cashcov to Python (and any ctypes/cffi consumer) |

---

## Three independent policy axes

### 1. MissFillPolicy

```go
cache.MissFillDefault      // zero value → behaves as MissFillSync
cache.MissFillSync         // lock → double-check → generate → write → return
cache.MissFillAsync        // generate → return; write to Redis in background goroutine
cache.MissFillStaleOrSync  // return stale immediately + background refresh; fallback to Sync
cache.MissFillFailFast     // return ErrCacheMiss without calling generator
cache.MissFillCooperative  // first caller generates; others block up to timeout, then generate directly
```

Set handler default with `WithMissFillPolicy(p)`.
Override per-call with `WithCallMissFillPolicy(p)`.

### 2. HitRefreshPolicy

```go
cache.HitRefreshDefault       // background refresh every hit, gated by refreshCooldown
cache.HitRefreshAhead         // refresh when remaining TTL ≤ threshold × originalTTL
cache.HitRefreshProbabilistic // XFetch algorithm — probabilistic early refresh
cache.HitRefreshOlderThan     // refresh when entry age ≥ configured duration
cache.HitRefreshNone          // no background refresh
```

Set handler default with `WithDefaultHitRefreshPolicy(p)`.
Override per-call with `WithCallHitRefreshPolicy(p)`.

### 3. ErrorPolicy

```go
cache.ErrorPolicySurface    // return error to caller (default)
cache.ErrorPolicyZeroValue  // suppress error; return zero value of T
```

`ErrCacheMiss` from `MissFillFailFast` is **never** suppressed by
`ErrorPolicyZeroValue`.

Set handler default with `WithDefaultErrorPolicy(p)`.
Override per-call with `WithCallErrorPolicy(p)`.

---

## Creating a handler

```go
import (
    "time"
    cache "github.com/Hossein-Roshandel/cashcov"
    "github.com/redis/go-redis/v9"
)

rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})

h, err := cache.New[Product](rdb,
    cache.WithPrefix("products"),
    cache.WithDefaultTTL(5 * time.Minute),
    cache.WithMissFillPolicy(cache.MissFillSync),
)
if err != nil {
    return fmt.Errorf("cache init: %w", err)
}
```

`New[T]` accepts any JSON-serialisable type, including structs, slices, and
maps. Use `Handler[string]` when the Python shim is involved (all cross-language
values must be JSON strings).

---

## GetOrRefresh

```go
result, err := h.GetOrRefresh(ctx, key, func(ctx context.Context) (Product, error) {
    return db.GetProduct(ctx, id)
})
// result.Value     — the value (from cache or freshly generated)
// result.FromCache — true if the value came from Redis
// result.CachedAt  — best-effort time of the write
```

Per-call options:

```go
h.GetOrRefresh(ctx, key, gen,
    cache.WithCallMissFillPolicy(cache.MissFillFailFast),
    cache.WithCallHitRefreshPolicy(cache.HitRefreshNone),
    cache.WithCallErrorPolicy(cache.ErrorPolicyZeroValue),
    cache.WithTTL(10*time.Minute),            // one-off TTL override
)
```

---

## Option reference

| Option constructor | Type | Notes |
|---|---|---|
| `WithPrefix(s)` | `Option` | Key prefix; applied as `prefix:key` |
| `WithDefaultTTL(d)` | `Option` | Default TTL; fallback 5 min if ≤ 0 |
| `WithMissFillPolicy(p)` | `Option` | Handler-level miss-fill default |
| `WithDefaultHitRefreshPolicy(p)` | `Option` | Handler-level hit-refresh default |
| `WithDefaultErrorPolicy(p)` | `Option` | Handler-level error default |
| `WithStaleDataTTL(d)` | `Option` | Required for `MissFillStaleOrSync` |
| `WithRefreshCooldown(d)` | `Option` | Min gap between hit-path background refreshes |
| `WithMissDeduplicationWindow(d)` | `Option` | Suppress duplicate async-miss generation within window |
| `WithRefreshAheadThreshold(f)` | `Option` | 0.0–1.0; required for `HitRefreshAhead` |
| `WithProbabilisticBeta(f)` | `Option` | Sensitivity for XFetch; default 1.0 |
| `WithRefreshOlderThanAge(d)` | `Option` | Required for `HitRefreshOlderThan` |
| `WithCooperativeTimeout(d)` | `Option` | Max block time for `MissFillCooperative` |
| `WithBackgroundRefreshTimeout(d)` | `Option` | Context timeout for background goroutines |
| `WithoutBackgroundRefresh()` | `CallOption` | Suppress all background goroutines for this call |
| `WithTTL(d)` | `CallOption` | Per-call TTL override |
| `WithCallMissFillPolicy(p)` | `CallOption` | Per-call miss-fill override |
| `WithCallHitRefreshPolicy(p)` | `CallOption` | Per-call hit-refresh override |
| `WithCallErrorPolicy(p)` | `CallOption` | Per-call error override |
| `WithCallRefreshOlderThanAge(d)` | `CallOption` | Per-call age threshold override |

---

## Policy combinations quick-reference

```go
// Stampede-safe synchronous fill (safe default)
cache.New[T](rdb, cache.WithPrefix("ns"), cache.WithDefaultTTL(5*time.Minute))

// Low-latency async write with duplicate suppression
cache.New[T](rdb,
    cache.WithMissFillPolicy(cache.MissFillAsync),
    cache.WithMissDeduplicationWindow(5*time.Second),
)

// Stale-while-revalidate
cache.New[T](rdb,
    cache.WithMissFillPolicy(cache.MissFillStaleOrSync),
    cache.WithStaleDataTTL(24*time.Hour),  // required
)

// Refresh-ahead (proactive TTL management)
cache.New[T](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshAhead),
    cache.WithRefreshAheadThreshold(0.2),  // refresh when 20% TTL remains
    cache.WithRefreshCooldown(30*time.Second),
)

// Refresh entries older than N minutes
cache.New[T](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshOlderThan),
    cache.WithRefreshOlderThanAge(10*time.Minute),
)

// Probabilistic (XFetch) — distributed refresh load, no coordination
cache.New[T](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshProbabilistic),
    cache.WithProbabilisticBeta(1.0),
)

// Circuit-breaker / explicit fallback — never call the generator
cache.New[T](rdb, cache.WithMissFillPolicy(cache.MissFillFailFast))
// Handle ErrCacheMiss:
result, err := h.GetOrRefresh(ctx, key, gen)
if errors.Is(err, cache.ErrCacheMiss) {
    // use fallback data source
}

// Suppress errors for non-critical data
cache.New[T](rdb, cache.WithDefaultErrorPolicy(cache.ErrorPolicyZeroValue))
```

---

## Background goroutines

The following code paths spawn goroutines **after** `GetOrRefresh` returns:

| Path | When |
|---|---|
| `spawnBackgroundMissWrite` | `MissFillAsync` — writes the generated value to Redis |
| `spawnBackgroundRefresh` | `HitRefreshDefault/Ahead/Probabilistic/OlderThan` — refreshes on a hit |
| `spawnStaleRefresh` | `MissFillStaleOrSync` stale path — writes fresh value to main key |

All background goroutines:
- Run under `context.WithTimeout(context.Background(), h.config.bgRefreshTimeout)`.
- Use `TryLock` (not `Lock`) — if the key is already being written they exit immediately.
- Double-check Redis before writing to avoid redundant writes.
- Suppress all errors (non-blocking by design).
- Can be suppressed for a specific call with `cache.WithoutBackgroundRefresh()`.

---

## Key naming

Redis keys are stored as `{prefix}:{key}`. The stale key for
`MissFillStaleOrSync` is `{prefix}:{key}:stale`. The `fullKey(key)` helper
applies the prefix.

---

## CGo shim (`cshim/shim.go`)

The shim exposes cashcov to Python (and any ctypes/cffi consumer) as a C
shared library. Key exported functions:

```c
int64_t CashCov_NewHandler(const char* redisAddr, const char* configJSON);
void    CashCov_SetGenerator(int64_t handle, cashcov_generator_fn gen);
char*   CashCov_GetOrRefresh(int64_t handle, const char* key,
            cashcov_generator_fn gen,
            int missFillPolicy, int hitRefreshPolicy, int errorPolicy);
int     CashCov_Set(int64_t handle, const char* key, const char* value, int ttlSecs);
void    CashCov_Free(char* ptr);
void    CashCov_DestroyHandler(int64_t handle);
```

Policy integers map directly onto the Go iota constants (0-based):

```
MissFillPolicy:   0=Default 1=Sync 2=Async 3=StaleOrSync 4=FailFast 5=Cooperative
HitRefreshPolicy: 0=Default 1=Ahead 2=Probabilistic 3=OlderThan 4=None
ErrorPolicy:      0=Surface 1=ZeroValue
```

Pass `-1` for policy arguments to use the handler default.

Shim validation: `CashCov_NewHandler` returns `-1` for out-of-range policy
integers, invalid JSON config, or Redis client creation failure.

Build:

```bash
go build -buildmode=c-shared -o /workspace/python/cashcov/libcashcov.so /workspace/cshim
```

---

## What NOT to do

```go
// WRONG — reusing a handler after Redis client is closed
rdb.Close()
h.GetOrRefresh(ctx, key, gen) // all operations will error

// WRONG — HitRefreshAhead without threshold
cache.New[T](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshAhead),
    // missing WithRefreshAheadThreshold — threshold defaults to 0; condition never fires
)

// WRONG — MissFillStaleOrSync without stale TTL
cache.New[T](rdb,
    cache.WithMissFillPolicy(cache.MissFillStaleOrSync),
    // missing WithStaleDataTTL — stale lookup always misses; behaves as MissFillSync
)

// WRONG — sharing one handler across types that have different Redis key spaces
// Use separate handlers with distinct WithPrefix values.

// WRONG — calling Set with a pre-JSON-encoded value
h.Set(ctx, "k", `{"already":"encoded"}`)
// cashcov json.Marshals the value again on write: Redis stores "\"{ \\\"already\\\"...}\""
// Correct: pass the Go value directly; let cashcov handle serialisation.
type Payload struct { Already string }
h.Set(ctx, "k", Payload{Already: "encoded"})

// WRONG — storing pointer types in T
// cache.New[*MyStruct] — GetOrRefresh returns a new *MyStruct on each call
// (json.Unmarshal allocates). This is fine, but the original pointer is never
// reused. T should be a value type or an interface for predictable behaviour.

// WRONG — ignoring ErrCacheMiss from MissFillFailFast
result, err := h.GetOrRefresh(ctx, key, gen)
if err != nil { return err }   // ErrCacheMiss is an expected signal, not a bug
// Correct: errors.Is(err, cache.ErrCacheMiss)
```

---

## Testing

```bash
# Unit + integration tests (requires Docker for testcontainers Redis)
make test

# Lint
make lint

# Format
make fmt
```

The test suite uses real Redis (testcontainers). Key patterns in `cache_test.go`:

- Seed Redis directly with `rdb.Set(ctx, fullKey, json.Marshal(v), ttl)` to
  test hit paths without going through the generator.
- Assert `result.FromCache` to distinguish cache hits from generator calls.
- For background goroutines, sleep briefly or poll Redis for the expected value.
- `WithoutBackgroundRefresh()` is useful in tests to make the hit path
  synchronous and deterministic.
