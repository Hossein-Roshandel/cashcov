package cache

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"time"

	"github.com/redis/go-redis/v9"
)

// ---------------------------
// Miss Helpers
// ---------------------------

func (h *Handler[T]) missSyncWriteThenReturn(ctx context.Context, key string, ttl time.Duration, gen Generator[T]) (Result[T], error) {
	var zero T
	fullKey := h.fullKey(key)

	// Acquire per-key lock
	unlock := h.localLocks.Lock(fullKey)
	defer unlock()

	// Double-check after acquiring lock
	if res, err := h.Get(ctx, key); err == nil {
		return res, nil
	} else if !errors.Is(err, redis.Nil) {
		return Result[T]{Value: zero}, err
	}

	// Still missing; generate and write
	v, err := gen(ctx)
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
	}
	if err := h.Set(ctx, key, v, WithTTL(ttl)); err != nil {
		return Result[T]{Value: zero}, err
	}
	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// missReturnThenAsyncWrite implements the async write miss policy
func (h *Handler[T]) missReturnThenAsyncWrite(ctx context.Context, key string, ttl time.Duration, gen Generator[T]) (Result[T], error) {
	var zero T
	v, err := gen(ctx)
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
	}
	go h.spawnBackgroundMissWrite(key, ttl, v)
	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// spawnBackgroundMissWrite persists a generated value in the background after a MISS.
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

// missStaleWhileRevalidate checks for stale data and returns it while refreshing in background
func (h *Handler[T]) missStaleWhileRevalidate(ctx context.Context, key string, ttl time.Duration, gen Generator[T], co callOpts) (Result[T], error) {
	var zero T
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

// missFailFast immediately returns an error without generating data
func (h *Handler[T]) missFailFast(ctx context.Context, key string) (Result[T], error) {
	var zero T
	return Result[T]{Value: zero, FromCache: false}, ErrCacheMissFailFast
}

// missRefreshAhead implements proactive refresh when TTL is low
func (h *Handler[T]) missRefreshAhead(ctx context.Context, key string, ttl time.Duration, gen Generator[T], co callOpts) (Result[T], error) {
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

// missCooperativeRefresh makes concurrent requests wait for the first one to complete
func (h *Handler[T]) missCooperativeRefresh(ctx context.Context, key string, ttl time.Duration, gen Generator[T]) (Result[T], error) {
	var zero T
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
		v, err := gen(ctx)
		if err != nil {
			return Result[T]{Value: zero}, fmt.Errorf("generator: %w", err)
		}
		return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
	case <-done:
		// Got lock, proceed with normal sync generation
		return h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	}
}

// missBestEffort attempts generation but returns zero value on error
func (h *Handler[T]) missBestEffort(ctx context.Context, key string, ttl time.Duration, gen Generator[T]) (Result[T], error) {
	var zero T
	
	v, err := gen(ctx)
	if err != nil {
		// Return zero value instead of error (graceful degradation)
		return Result[T]{Value: zero, FromCache: false, CachedAt: time.Now()}, nil
	}
	
	// Successfully generated, write to cache
	if setErr := h.Set(ctx, key, v, WithTTL(ttl)); setErr != nil {
		// Generation succeeded but cache write failed, still return the value
		return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
	}
	
	return Result[T]{Value: v, FromCache: false, CachedAt: time.Now()}, nil
}

// missProbabilisticRefresh uses probabilistic early expiration
func (h *Handler[T]) missProbabilisticRefresh(ctx context.Context, key string, ttl time.Duration, gen Generator[T], co callOpts) (Result[T], error) {
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

// getFromKey gets data from a specific Redis key (for stale data)
func (h *Handler[T]) getFromKey(ctx context.Context, fullKey string) (T, error) {
	var zero T
	
	cmd := h.config.rdb.Get(ctx, fullKey)
	if err := cmd.Err(); err != nil {
		return zero, err
	}
	
	raw, err := cmd.Bytes()
	if err != nil {
		return zero, err
	}
	
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		return zero, err
	}
	
	return v, nil
}

// spawnStaleRefresh refreshes data and updates both main and stale keys
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

// setToKey sets data to a specific Redis key
func (h *Handler[T]) setToKey(ctx context.Context, fullKey string, value T, ttl time.Duration) error {
	b, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return h.config.rdb.Set(ctx, fullKey, b, ttl).Err()
}

// scheduleRefreshAhead sets up proactive refresh monitoring
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

// enableProbabilisticRefresh sets up probabilistic refresh logic
func (h *Handler[T]) enableProbabilisticRefresh(key string, ttl time.Duration, gen Generator[T], beta float64) {
	// This would typically be implemented with a background worker
	// For now, it sets up the framework for probabilistic refresh
	// The actual probabilistic logic would be checked on cache hits
	
	// Store metadata for probabilistic calculation
	fullKey := h.fullKey(key)
	h.lastRefreshMu.Lock()
	h.lastRefreshByKey[fullKey+"@created"] = time.Now()
	h.lastRefreshMu.Unlock()
}

// shouldProbabilisticRefresh calculates if probabilistic refresh should occur
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
	return rand.Float64() < probability
}

// handleHitRefresh manages different refresh strategies on cache hits
func (h *Handler[T]) handleHitRefresh(ctx context.Context, key string, ttl time.Duration, gen Generator[T], missPolicy MissPolicy, co callOpts) {
	fullKey := h.fullKey(key)
	
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
		if h.shouldRefreshNow(fullKey) {
			go h.spawnBackgroundRefresh(key, ttl, gen)
		}
	}
}

// shouldRefreshAhead checks if refresh-ahead should trigger based on remaining TTL
func (h *Handler[T]) shouldRefreshAhead(ctx context.Context, fullKey string, originalTTL time.Duration, threshold float64) bool {
	// Get remaining TTL from Redis
	remaining, err := h.config.rdb.TTL(ctx, fullKey).Result()
	if err != nil || remaining <= 0 {
		return false
	}
	
	// Calculate if remaining TTL is below threshold
	remainingRatio := float64(remaining) / float64(originalTTL)
	return remainingRatio <= threshold
}
