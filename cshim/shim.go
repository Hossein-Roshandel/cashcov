// Package main is a CGo shim that exposes the cashcov cache library as a
// C shared library (.so / .dylib / .dll). All cached values are treated as
// JSON strings so that any language can serialise and deserialise its own
// types. Python (and any other ctypes/cffi consumer) should JSON-encode
// values before writing and JSON-decode them after reading.
//
// Build:
//
//	go build -buildmode=c-shared -o libcashcov.so ./cshim
//
// The build emits two files: libcashcov.so (the shared library) and
// libcashcov.h (a generated C header). The Python package bundles the .so
// and uses ctypes to load it at runtime.
package main

/*
#include <stdlib.h>

// Generator callback type.  Python passes a ctypes.CFUNCTYPE pointer.
// The function receives the cache key and must return a newly-allocated
// C string containing the JSON-encoded value, or NULL on error.
// cashcov takes ownership of the returned string and will free it.
typedef char* (*cashcov_generator_fn)(const char* key);

// Trampoline: calling a Go-held C function pointer requires a C helper
// because cgo does not allow direct calls to arbitrary C function pointers.
static char* call_generator(cashcov_generator_fn fn, const char* key) {
    return fn(key);
}
*/
import "C"

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	cache "github.com/Hossein-Roshandel/cashcov"
	"github.com/redis/go-redis/v9"
)

// ---------------------------------------------------------------------------
// Handler config struct — decoded from the JSON passed to CashCov_NewHandler
//
// All fields are optional; zero values mean "use library default".
// Policy fields map 1-to-1 onto the Go iota constants:
//
//	MissFillPolicy:    0=Default 1=Sync 2=Async 3=StaleOrSync 4=FailFast 5=Cooperative
//	HitRefreshPolicy:  0=Default 1=Ahead 2=Probabilistic 3=OlderThan 4=None
//	ErrorPolicy:       0=Surface 1=ZeroValue
// ---------------------------------------------------------------------------

type shimHandlerConfig struct {
	Prefix                string  `json:"prefix"`
	TTLSecs               int     `json:"ttl_secs"`
	MissFillPolicy        int     `json:"miss_fill_policy"`
	HitRefreshPolicy      int     `json:"hit_refresh_policy"`
	ErrorPolicy           int     `json:"error_policy"`
	StaleTTLSecs          int     `json:"stale_ttl_secs"`
	RefreshCooldownSecs   int     `json:"refresh_cooldown_secs"`
	DedupWindowSecs       int     `json:"dedup_window_secs"`
	CoopTimeoutSecs       int     `json:"cooperative_timeout_secs"`
	RefreshAheadThreshold float64 `json:"refresh_ahead_threshold"`
	RefreshOlderThanSecs  int     `json:"refresh_older_than_secs"`
	ProbabilisticBeta     float64 `json:"probabilistic_beta"`
}

// ---------------------------------------------------------------------------
// Handle registry
// A Handler[string] is heap-allocated and stored behind an integer handle so
// that C / Python callers never touch Go memory directly.
// ---------------------------------------------------------------------------

var (
	handleMu sync.RWMutex
	handles  = map[int64]*cache.Handler[string]{}
	seq      int64
)

func storeHandle(h *cache.Handler[string]) int64 {
	id := atomic.AddInt64(&seq, 1)
	handleMu.Lock()
	handles[id] = h
	handleMu.Unlock()
	return id
}

func loadHandle(id int64) (*cache.Handler[string], bool) {
	handleMu.RLock()
	h, ok := handles[id]
	handleMu.RUnlock()
	return h, ok
}

func deleteHandle(id int64) {
	handleMu.Lock()
	delete(handles, id)
	handleMu.Unlock()
}

// ---------------------------------------------------------------------------
// Exported C functions
// ---------------------------------------------------------------------------

// CashCov_NewHandler creates a new cache handler and returns an opaque integer
// handle, or -1 on error.
//
// configJSON is a JSON object with all handler options. Only redisAddr is
// required. Example:
//
//	{
//	  "prefix":              "myapp",
//	  "ttl_secs":            300,
//	  "miss_fill_policy":    2,
//	  "hit_refresh_policy":  1,
//	  "error_policy":        0,
//	  "stale_ttl_secs":      86400,
//	  "refresh_cooldown_secs": 30,
//	  "dedup_window_secs":   5,
//	  "cooperative_timeout_secs": 10,
//	  "refresh_ahead_threshold": 0.2,
//	  "refresh_older_than_secs": 600,
//	  "probabilistic_beta":  1.0
//	}
//
//export CashCov_NewHandler
func CashCov_NewHandler(redisAddr *C.char, configJSON *C.char) C.int64_t {
	if redisAddr == nil {
		return -1
	}

	var cfg shimHandlerConfig
	if configJSON != nil && C.GoString(configJSON) != "" {
		if err := json.Unmarshal([]byte(C.GoString(configJSON)), &cfg); err != nil {
			return -1
		}
	}

	rdb := redis.NewClient(&redis.Options{
		Addr: C.GoString(redisAddr),
	})

	ttl := cfg.TTLSecs
	if ttl <= 0 {
		ttl = 300 // 5-minute fallback
	}

	opts := []cache.Option{
		cache.WithPrefix(cfg.Prefix),
		cache.WithDefaultTTL(time.Duration(ttl) * time.Second),
		cache.WithMissFillPolicy(cache.MissFillPolicy(cfg.MissFillPolicy)),
		cache.WithDefaultHitRefreshPolicy(cache.HitRefreshPolicy(cfg.HitRefreshPolicy)),
		cache.WithDefaultErrorPolicy(cache.ErrorPolicy(cfg.ErrorPolicy)),
	}
	if cfg.StaleTTLSecs > 0 {
		opts = append(opts, cache.WithStaleDataTTL(time.Duration(cfg.StaleTTLSecs)*time.Second))
	}
	if cfg.RefreshCooldownSecs > 0 {
		opts = append(opts, cache.WithRefreshCooldown(time.Duration(cfg.RefreshCooldownSecs)*time.Second))
	}
	if cfg.DedupWindowSecs > 0 {
		opts = append(opts, cache.WithMissDeduplicationWindow(time.Duration(cfg.DedupWindowSecs)*time.Second))
	}
	if cfg.CoopTimeoutSecs > 0 {
		opts = append(opts, cache.WithCooperativeTimeout(time.Duration(cfg.CoopTimeoutSecs)*time.Second))
	}
	if cfg.RefreshAheadThreshold > 0 {
		opts = append(opts, cache.WithRefreshAheadThreshold(cfg.RefreshAheadThreshold))
	}
	if cfg.RefreshOlderThanSecs > 0 {
		opts = append(opts, cache.WithRefreshOlderThanAge(time.Duration(cfg.RefreshOlderThanSecs)*time.Second))
	}
	if cfg.ProbabilisticBeta > 0 {
		opts = append(opts, cache.WithProbabilisticBeta(cfg.ProbabilisticBeta))
	}

	h, err := cache.New[string](rdb, opts...)
	if err != nil {
		return -1
	}
	return C.int64_t(storeHandle(h))
}

// CashCov_GetOrRefresh looks up key in the cache. On a miss it invokes the
// supplied generator callback to produce a fresh value, writes it to Redis,
// and returns it. On a hit the cached value is returned directly.
//
// missFillPolicy, hitRefreshPolicy, errorPolicy are per-call policy overrides.
// Pass -1 for any of them to use the handler's configured default.
//
// The returned pointer is a newly-allocated C string that the caller MUST
// release with CashCov_Free. Returns NULL on error.
//
// The generator callback receives the cache key and must return a
// newly-allocated (malloc'd) C string with the JSON-encoded value, or NULL
// to signal a generation failure. cashcov frees the returned string.
//
//export CashCov_GetOrRefresh
func CashCov_GetOrRefresh(
	handle C.int64_t,
	key *C.char,
	gen C.cashcov_generator_fn,
	missFillPolicy C.int,
	hitRefreshPolicy C.int,
	errorPolicy C.int,
) *C.char {
	if key == nil || gen == nil {
		return nil
	}
	h, ok := loadHandle(int64(handle))
	if !ok {
		return nil
	}

	goKey := C.GoString(key)

	generator := func(_ context.Context) (string, error) {
		cResult := C.call_generator(gen, C.CString(goKey))
		if cResult == nil {
			return "", fmt.Errorf("generator returned nil for key %q", goKey)
		}
		// Copy into Go memory before freeing the C allocation.
		goResult := C.GoString(cResult)
		C.free(unsafe.Pointer(cResult))
		return goResult, nil
	}

	var callOpts []cache.CallOption
	if missFillPolicy >= 0 {
		p := cache.MissFillPolicy(missFillPolicy)
		callOpts = append(callOpts, cache.WithCallMissFillPolicy(p))
	}
	if hitRefreshPolicy >= 0 {
		p := cache.HitRefreshPolicy(hitRefreshPolicy)
		callOpts = append(callOpts, cache.WithCallHitRefreshPolicy(p))
	}
	if errorPolicy >= 0 {
		p := cache.ErrorPolicy(errorPolicy)
		callOpts = append(callOpts, cache.WithCallErrorPolicy(p))
	}

	res, err := h.GetOrRefresh(context.Background(), goKey, generator, callOpts...)
	if err != nil {
		return nil
	}
	return C.CString(res.Value)
}

// CashCov_Set writes value (JSON string) to the cache under key with an
// explicit TTL.  Pass ttlSecs = 0 to use the handler's default TTL.
// Returns 0 on success, -1 on error.
//
//export CashCov_Set
func CashCov_Set(handle C.int64_t, key *C.char, value *C.char, ttlSecs C.int) C.int {
	if key == nil || value == nil {
		return -1
	}
	h, ok := loadHandle(int64(handle))
	if !ok {
		return -1
	}

	var opts []cache.CallOption
	if ttlSecs > 0 {
		opts = append(opts, cache.WithTTL(time.Duration(ttlSecs)*time.Second))
	}

	if err := h.Set(context.Background(), C.GoString(key), C.GoString(value), opts...); err != nil {
		return -1
	}
	return 0
}

// CashCov_DestroyHandler releases all resources held by the handle.
// The handle must not be used after this call.
//
//export CashCov_DestroyHandler
func CashCov_DestroyHandler(handle C.int64_t) {
	deleteHandle(int64(handle))
}

// CashCov_Free releases a C string previously returned by this library.
// Every non-NULL string returned by CashCov_GetOrRefresh must be freed
// exactly once with this function.
//
//export CashCov_Free
func CashCov_Free(ptr *C.char) {
	C.free(unsafe.Pointer(ptr))
}

// main is required by the c-shared build mode but is never called.
func main() {}
