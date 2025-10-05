package cache

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/redis/go-redis/v9"
)

// ---------------------------
// Miss Helpers
// ---------------------------

// missSyncWriteThenReturn handles a cache miss by synchronously generating a value and writing it to the cache.
// It acquires a per-key lock to prevent concurrent writes, double-checks the cache after locking, and generates
// the value using the provided Generator if still missing. On generation error or cache write failure, it returns
// a zero-valued Result with the error. On success, it returns the generated value with FromCache set to false.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to check and store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//
// Returns:
//   - Result[T]: The result containing the generated value or a zero value on error.
//   - error: Any error from the cache check, generation, or cache write.
func (h *Handler[T]) missSyncWriteThenReturn(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
) (Result[T], error) {
	var err error
	var v T
	var res Result[T]
	var zero T
	fullKey := h.fullKey(key)

	// Acquire per-key lock
	unlock := h.localLocks.Lock(fullKey)
	defer unlock()

	// Double-check after acquiring lock
	if res, err = h.Get(ctx, key); err == nil {
		return res, nil
	} else if !errors.Is(err, redis.Nil) {
		return Result[T]{Value: zero}, err
	}

	// Still missing; generate and write
	v, err = gen(ctx)
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
	}
	if err = h.Set(ctx, key, v, WithTTL(ttl)); err != nil {
		return Result[T]{Value: zero}, err
	}
	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// missReturnThenAsyncWrite handles a cache miss by generating a value and returning it immediately,
// while asynchronously writing it to the cache in the background. On generation error, it returns
// a zero-valued Result with the wrapped error. The background write is performed by spawnBackgroundMissWrite.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts during generation.
//   - key: Cache key to store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//
// Returns:
//   - Result[T]: The result containing the generated value or a zero value on error.
//   - error: Any error from the value generation.
func (h *Handler[T]) missReturnThenAsyncWrite(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
) (Result[T], error) {
	var zero T
	v, err := gen(ctx)
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
	}
	go h.spawnBackgroundMissWrite(key, ttl, v)
	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// spawnBackgroundMissWrite persists a generated value to the cache in the background after a cache miss.
// It uses a try-lock to avoid concurrent writes, double-checks if the key is already present, and writes
// the value to Redis with the specified TTL if the key is still missing. The operation respects the
// configured background refresh timeout (bgRefreshTimeout). Errors are ignored to ensure non-blocking behavior.
//
// Parameters:
//   - key: Cache key to store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - v: The value to cache.
func (h *Handler[T]) spawnBackgroundMissWrite(key string, ttl time.Duration, v T) {
	ctx, cancel := context.WithTimeout(context.Background(), h.config.bgRefreshTimeout)
	defer cancel()

	fullKey := h.fullKey(key)

	// Try-lock: if someone else is writing, skip.
	unlock, ok := h.localLocks.TryLock(fullKey)
	if !ok {
		return
	}
	defer unlock()

	// Double-check if key is now present.
	exists, err := h.config.rdb.Exists(ctx, fullKey).Result()
	if err != nil || exists > 0 {
		return
	}

	_ = h.Set(ctx, key, v, WithTTL(ttl))
}

// ---------------------------
// Hit Refresh Helper
// ---------------------------

// spawnBackgroundRefresh refreshes a cache entry in the background on a cache hit.
// It uses a try-lock to avoid concurrent refreshes, respects the configured refresh cooldown,
// and generates a new value using the provided Generator, updating the cache with the new value.
// The operation respects the configured background refresh timeout (bgRefreshTimeout). Errors
// are ignored to ensure non-blocking behavior.
//
// Parameters:
//   - key: Cache key to refresh.
//   - ttl: Time-to-live duration for the updated value.
//   - gen: Generator function to produce the new value.
func (h *Handler[T]) spawnBackgroundRefresh(key string, ttl time.Duration, gen Generator[T]) {
	ctx, cancel := context.WithTimeout(context.Background(), h.config.bgRefreshTimeout)
	defer cancel()

	fullKey := h.fullKey(key)

	// Try-lock: if someone else is refreshing, skip.
	unlock, ok := h.localLocks.TryLock(fullKey)
	if !ok {
		return
	}
	defer unlock()

	// Respect refresh cooldown on HIT-path
	if !h.shouldRefreshNow(fullKey) {
		return
	}

	// Generate and update
	v, err := gen(ctx)
	if err != nil {
		return
	}
	_ = h.Set(ctx, key, v, WithTTL(ttl))
}

// ---------------------------
// Cooldown Tracking
// ---------------------------

// shouldRefreshNow checks if a cache key is eligible for refresh based on the configured cooldown.
// It returns true if the refresh cooldown is zero or if the time since the last refresh exceeds
// the cooldown duration. The check is thread-safe using lastRefreshMu.
//
// Parameters:
//   - fullKey: The full cache key (including prefix) to check.
//
// Returns:
//   - bool: True if the key can be refreshed, false otherwise.
func (h *Handler[T]) shouldRefreshNow(fullKey string) bool {
	if h.config.refreshCooldown <= 0 {
		return true
	}
	h.lastRefreshMu.Lock()
	defer h.lastRefreshMu.Unlock()
	last, ok := h.lastRefreshByKey[fullKey]
	if !ok {
		return true
	}
	return time.Since(last) >= h.config.refreshCooldown
}

// setLastRefreshNow records the current time as the last refresh time for a cache key.
// It updates the lastRefreshByKey map in a thread-safe manner using lastRefreshMu.
// If the refresh cooldown is zero, the operation is a no-op.
//
// Parameters:
//   - fullKey: The full cache key (including prefix) to update.
func (h *Handler[T]) setLastRefreshNow(fullKey string) {
	if h.config.refreshCooldown <= 0 {
		return
	}
	h.lastRefreshMu.Lock()
	h.lastRefreshByKey[fullKey] = time.Now()
	h.lastRefreshMu.Unlock()
}

// ---------------------------
// New Miss Policy Handlers
// ---------------------------

// missStaleWhileRevalidate handles a cache miss by checking for stale data and returning it,
// while refreshing the main cache in the background. If no stale data is found, it falls back
// to synchronous generation via missSyncWriteThenReturn. The stale check respects the provided
// staleCheckTimeout (defaulting to 1 second if zero or negative). Background refresh is performed
// by spawnStaleRefresh.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to check and store the value.
//   - ttl: Time-to-live duration for the main cache entry.
//   - gen: Generator function to produce the value on cache miss.
//   - co: Call options, including staleCheckTimeout.
//
// Returns:
//   - Result[T]: The result containing stale data (if available) or a synchronously generated value.
//   - error: Any error from the stale data check or synchronous generation.
func (h *Handler[T]) missStaleWhileRevalidate(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
	co callOpts,
) (Result[T], error) {
	staleKey := h.fullKey(key + ":stale")

	// Check for stale data
	staleTimeout := co.staleCheckTimeout
	if staleTimeout <= 0 {
		staleTimeout = 1 * time.Second
	}

	staleCtx, cancel := context.WithTimeout(ctx, staleTimeout)
	defer cancel()

	if staleResult, err := h.getFromKey(staleCtx, staleKey); err == nil {
		// Found stale data, return it and refresh in background
		go h.spawnStaleRefresh(key, ttl, gen)
		return Result[T]{Value: staleResult, FromCache: true, CachedAt: time.Now()}, nil
	}

	// No stale data, fall back to sync generation
	return h.missSyncWriteThenReturn(ctx, key, ttl, gen)
}

// missFailFast handles a cache miss by immediately returning an error without attempting generation.
// It returns a zero-valued Result with ErrCacheMissFailFast, suitable for policies requiring fast failure.
//
// Parameters:
//   - ctx: Context (unused, provided for consistency).
//   - key: Cache key (unused, provided for consistency).
//
// Returns:
//   - Result[T]: A zero-valued Result with FromCache set to false.
//   - error: Always returns ErrCacheMissFailFast.
func (h *Handler[T]) missFailFast(_ context.Context, _ string) (Result[T], error) {
	var zero T
	return Result[T]{Value: zero, FromCache: false}, ErrCacheMissFailFast
}

// missRefreshAhead handles a cache miss by performing synchronous generation and scheduling a proactive
// refresh for future cache hits. It uses missSyncWriteThenReturn for the initial miss and schedules a
// background refresh via scheduleRefreshAhead based on the provided refreshAheadThreshold (defaulting to
// the configured defaultRefreshAheadThreshold if zero or negative).
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to check and store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//   - co: Call options, including refreshAheadThreshold.
//
// Returns:
//   - Result[T]: The result from synchronous generation.
//   - error: Any error from synchronous generation.
func (h *Handler[T]) missRefreshAhead(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
	co callOpts,
) (Result[T], error) {
	// On actual miss, behave like sync write
	result, err := h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	if err != nil {
		return result, err
	}

	// Schedule refresh-ahead for future hits
	threshold := co.refreshAheadThreshold
	if threshold <= 0 {
		threshold = h.config.defaultRefreshAheadThreshold
	}

	go h.scheduleRefreshAhead(key, ttl, gen, threshold)
	return result, nil
}

// missCooperativeRefresh handles a cache miss by allowing concurrent requests to wait for the first
// request to complete generation, using a lock with a timeout (cooperativeTimeout). If the lock is
// acquired, it performs synchronous generation via missSyncWriteThenReturn. If the lock times out,
// it generates the value immediately without caching to avoid blocking.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to check and store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//
// Returns:
//   - Result[T]: The result containing the generated value (from sync or immediate generation).
//   - error: Any error from the value generation.
func (h *Handler[T]) missCooperativeRefresh(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
) (Result[T], error) {
	var zero T
	var err error
	var v T
	fullKey := h.fullKey(key)

	// Try to acquire lock with timeout
	lockCtx, cancel := context.WithTimeout(ctx, h.config.cooperativeTimeout)
	defer cancel()

	done := make(chan struct{})
	go func() {
		unlock := h.localLocks.Lock(fullKey)
		defer unlock()
		close(done)
	}()

	select {
	case <-lockCtx.Done():
		// Timeout waiting for lock, fall back to immediate generation
		v, err = gen(ctx)
		if err != nil {
			return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
		}
		return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
	case <-done:
		// Got lock, proceed with normal sync generation
		return h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	}
}

// missBestEffort handles a cache miss by attempting to generate a value and returning it,
// ignoring any cache write failures. On generation error, it returns a zero-valued Result
// with no error. Cache write errors are ignored by design, ensuring the generated value is
// returned regardless of caching success.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//
// Returns:
//   - Result[T]: The result containing the generated value or a zero value on generation error.
//   - error: Always nil, as errors are ignored by design.
func (h *Handler[T]) missBestEffort(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
) (Result[T], error) {
	var zero T
	var err error
	var v T

	v, err = gen(ctx)
	//nolint:nilerr // Cache write failure is ignored by design, return generated value
	if err != nil {
		return Result[T]{
			Value:     zero,
			FromCache: false,
			CachedAt:  time.Now(),
		}, nil
	}

	// Successfully generated, write to cache
	//nolint:nilerr // Cache write failure is ignored by design, return generated value
	if setErr := h.Set(ctx, key, v, WithTTL(ttl)); setErr != nil {
		return Result[T]{
			Value:     v,
			FromCache: false,
			CachedAt:  time.Now(),
		}, nil
	}

	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// missProbabilisticRefresh handles a cache miss by performing synchronous generation and
// enabling probabilistic refresh for future cache hits. It uses missSyncWriteThenReturn for
// the initial miss and sets up probabilistic refresh metadata via enableProbabilisticRefresh,
// using the provided probabilisticRefreshBeta (defaulting to defaultProbabilisticBeta if zero
// or negative).
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to check and store the value.
//   - ttl: Time-to-live duration for the cached value.
//   - gen: Generator function to produce the value on cache miss.
//   - co: Call options, including probabilisticRefreshBeta.
//
// Returns:
//   - Result[T]: The result from synchronous generation.
//   - error: Any error from synchronous generation.
func (h *Handler[T]) missProbabilisticRefresh(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
	co callOpts,
) (Result[T], error) {
	// For actual miss, use sync generation
	result, err := h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	if err != nil {
		return result, err
	}

	// Set up probabilistic refresh for future hits
	beta := co.probabilisticRefreshBeta
	if beta <= 0 {
		beta = h.config.defaultProbabilisticBeta
	}

	go h.enableProbabilisticRefresh(key, ttl, gen, beta)
	return result, nil
}

// ---------------------------
// Helper Methods for New Policies
// ---------------------------

// getFromKey retrieves a value from a specific Redis key, typically used for stale data.
// It fetches the raw bytes from Redis, unmarshals them into type T, and returns the value.
// On any error (Redis fetch or unmarshaling), it returns a zero value and the error.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - fullKey: The full Redis key (including prefix) to fetch.
//
// Returns:
//   - T: The unmarshaled value or a zero value on error.
//   - error: Any error from the Redis fetch or unmarshaling.
func (h *Handler[T]) getFromKey(ctx context.Context, fullKey string) (T, error) {
	var zero T
	var err error
	var raw []byte
	cmd := h.config.rdb.Get(ctx, fullKey)
	if err = cmd.Err(); err != nil {
		return zero, err
	}

	raw, err = cmd.Bytes()
	if err != nil {
		return zero, err
	}

	var v T
	if err = json.Unmarshal(raw, &v); err != nil {
		return zero, err
	}

	return v, nil
}

// spawnStaleRefresh refreshes both the main and stale cache keys in the background.
// It generates a new value using the provided Generator, updates the main key with the
// specified TTL, and updates the stale key with the configured staleDataTTL. It uses a
// try-lock to avoid concurrent refreshes and respects the background refresh timeout
// (bgRefreshTimeout). Errors are ignored to ensure non-blocking behavior.
//
// Parameters:
//   - key: Cache key to refresh (main and stale).
//   - ttl: Time-to-live duration for the main cache entry.
//   - gen: Generator function to produce the new value.
func (h *Handler[T]) spawnStaleRefresh(key string, ttl time.Duration, gen Generator[T]) {
	ctx, cancel := context.WithTimeout(context.Background(), h.config.bgRefreshTimeout)
	defer cancel()

	fullKey := h.fullKey(key)
	staleKey := h.fullKey(key + ":stale")

	unlock, ok := h.localLocks.TryLock(fullKey)
	if !ok {
		return
	}
	defer unlock()

	// Generate new data
	v, err := gen(ctx)
	if err != nil {
		return
	}

	// Update main key
	_ = h.Set(ctx, key, v, WithTTL(ttl))

	// Update stale key with longer TTL
	_ = h.setToKey(ctx, staleKey, v, h.config.staleDataTTL)
}

// setToKey sets a value to a specific Redis key with the specified TTL.
// It marshals the value to JSON and stores it in Redis, returning any error
// from marshaling or the Redis operation.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - fullKey: The full Redis key (including prefix) to set.
//   - value: The value to store.
//   - ttl: Time-to-live duration for the key.
//
// Returns:
//   - error: Any error from JSON marshaling or the Redis set operation.
func (h *Handler[T]) setToKey(ctx context.Context, fullKey string, value T, ttl time.Duration) error {
	b, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return h.config.rdb.Set(ctx, fullKey, b, ttl).Err()
}

// scheduleRefreshAhead schedules a proactive refresh for a cache key after a delay.
// It sleeps for a duration based on the TTL and refresh threshold, then checks if the
// key still exists in Redis. If it does, it triggers a background refresh via
// spawnBackgroundRefresh. The operation respects the background refresh timeout
// (bgRefreshTimeout). Errors are ignored to ensure non-blocking behavior.
//
// Parameters:
//   - key: Cache key to refresh.
//   - ttl: Time-to-live duration of the cache entry.
//   - gen: Generator function to produce the new value.
//   - threshold: Fraction of TTL remaining to trigger refresh (e.g., 0.2 for 20%).
func (h *Handler[T]) scheduleRefreshAhead(key string, ttl time.Duration, gen Generator[T], threshold float64) {
	refreshTime := time.Duration(float64(ttl) * (1.0 - threshold))
	time.Sleep(refreshTime)

	// Check if key still exists and refresh if needed
	ctx, cancel := context.WithTimeout(context.Background(), h.config.bgRefreshTimeout)
	defer cancel()

	fullKey := h.fullKey(key)
	exists, err := h.config.rdb.Exists(ctx, fullKey).Result()
	if err != nil || exists == 0 {
		return
	}

	h.spawnBackgroundRefresh(key, ttl, gen)
}

// enableProbabilisticRefresh initializes metadata for probabilistic refresh.
// It stores the creation time of the cache key in lastRefreshByKey, enabling
// future probabilistic refresh checks via shouldProbabilisticRefresh. This is a
// no-op for the actual refresh logic, which is handled on cache hits.
//
// Parameters:
//   - key: Cache key to track.
//   - ttl: Time-to-live duration (unused, provided for consistency).
//   - gen: Generator function (unused, provided for consistency).
//   - beta: Probabilistic refresh factor (unused, provided for consistency).// enableProbabilisticRefresh initializes metadata for probabilistic refresh.
//
// It stores the creation time of the cache key in lastRefreshByKey, enabling
// future probabilistic refresh checks via shouldProbabilisticRefresh. This is a
// no-op for the actual refresh logic, which is handled on cache hits.
//
// Parameters:
//   - key: Cache key to track.
//   - ttl: Time-to-live duration (unused, provided for consistency).
//   - gen: Generator function (unused, provided for consistency).
//   - beta: Probabilistic refresh factor (unused, provided for consistency).
func (h *Handler[T]) enableProbabilisticRefresh(key string, _ time.Duration, _ Generator[T], _ float64) {
	// This would typically be implemented with a background worker
	// For now, it sets up the framework for probabilistic refresh
	// The actual probabilistic logic would be checked on cache hits

	// Store metadata for probabilistic calculation
	fullKey := h.fullKey(key)
	h.lastRefreshMu.Lock()
	h.lastRefreshByKey[fullKey+"@created"] = time.Now()
	h.lastRefreshMu.Unlock()
}

// shouldProbabilisticRefresh determines if a cache key should be refreshed based on a
// probabilistic formula. It calculates the key’s age relative to its TTL and applies a
// probabilistic factor (beta) to decide if a refresh is needed. Returns true if a random
// value is less than (age/TTL) * beta, indicating a refresh should occur.
//
// Parameters:
//   - key: Cache key to check.
//   - ttl: Time-to-live duration of the cache entry.
//   - beta: Probabilistic refresh factor (higher values increase refresh likelihood).
//
// Returns:
//   - bool: True if a refresh should occur, false otherwise.
func (h *Handler[T]) shouldProbabilisticRefresh(key string, ttl time.Duration, beta float64) bool {
	fullKey := h.fullKey(key)

	h.lastRefreshMu.Lock()
	created, exists := h.lastRefreshByKey[fullKey+"@created"]
	h.lastRefreshMu.Unlock()

	if !exists {
		return false
	}

	age := time.Since(created)
	ageRatio := float64(age) / float64(ttl)

	// Probabilistic formula: random() < (age / ttl) * beta
	probability := ageRatio * beta
	return rand.Float64() < probability //nolint:gosec // This is not a security case, and a pseudo random is good enough
}

// handleHitRefresh manages refresh strategies for cache hits based on the miss policy.
// It triggers background refreshes for supported policies, respecting thresholds and
// cooldowns. The supported policies are:
//   - MissPolicyRefreshAhead: Refreshes if the remaining TTL is below the threshold.
//   - MissPolicyProbabilisticRefresh: Refreshes based on a probabilistic formula.
//   - Other policies (SyncWriteThenReturn, ReturnThenAsyncWrite, StaleWhileRevalidate,
//     FailFast, CooperativeRefresh, BestEffort): Perform standard background refresh
//     if the cooldown allows.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - key: Cache key to refresh.
//   - ttl: Time-to-live duration for the updated value.
//   - gen: Generator function to produce the new value.
//   - missPolicy: The cache miss policy determining the refresh strategy.
//   - co: Call options, including refreshAheadThreshold and probabilisticRefreshBeta.
func (h *Handler[T]) handleHitRefresh(
	ctx context.Context,
	key string,
	ttl time.Duration,
	gen Generator[T],
	missPolicy MissPolicy,
	co callOpts,
) {
	fullKey := h.fullKey(key)

	//nolint:exhaustive // Default case handles standard background refresh for unlisted policies
	switch missPolicy {
	case MissPolicyRefreshAhead:
		threshold := co.refreshAheadThreshold
		if threshold <= 0 {
			threshold = h.config.defaultRefreshAheadThreshold
		}
		if h.shouldRefreshAhead(ctx, fullKey, ttl, threshold) {
			go h.spawnBackgroundRefresh(key, ttl, gen)
		}

	case MissPolicyProbabilisticRefresh:
		beta := co.probabilisticRefreshBeta
		if beta <= 0 {
			beta = h.config.defaultProbabilisticBeta
		}
		if h.shouldProbabilisticRefresh(key, ttl, beta) {
			go h.spawnBackgroundRefresh(key, ttl, gen)
		}

	default:
		// Standard background refresh
		// MissPolicySyncWriteThenReturn
		// MissPolicyReturnThenAsyncWrite
		// MissPolicyStaleWhileRevalidate
		// MissPolicyFailFast
		// MissPolicyCooperativeRefresh
		// MissPolicyBestEffort

		if h.shouldRefreshNow(fullKey) {
			go h.spawnBackgroundRefresh(key, ttl, gen)
		}
	}
}

// shouldRefreshAhead checks if a proactive refresh should be triggered based on the
// remaining TTL of a cache key. It queries Redis for the key’s remaining TTL and returns
// true if the remaining TTL ratio (remaining/original) is below the specified threshold.
//
// Parameters:
//   - ctx: Context for cancellation and timeouts.
//   - fullKey: The full Redis key (including prefix) to check.
//   - originalTTL: The original time-to-live duration of the cache entry.
//   - threshold: Fraction of TTL remaining to trigger refresh (e.g., 0.2 for 20%).
//
// Returns:
//   - bool: True if a refresh should occur, false otherwise.
func (h *Handler[T]) shouldRefreshAhead(
	ctx context.Context,
	fullKey string,
	originalTTL time.Duration,
	threshold float64,
) bool {
	// Get remaining TTL from Redis
	remaining, err := h.config.rdb.TTL(ctx, fullKey).Result()
	if err != nil || remaining <= 0 {
		return false
	}

	// Calculate if remaining TTL is below threshold
	remainingRatio := float64(remaining) / float64(originalTTL)
	return remainingRatio <= threshold
}
