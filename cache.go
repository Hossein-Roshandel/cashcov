package cache

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// Handler is the Redis cache handler.
type Handler[T any] struct {
	config           handlerConfig
	localLocks       *keyedMutex
	lastRefreshByKey map[string]time.Time
	lastRefreshMu    sync.Mutex
}

// New creates a new cache Handler[T].
func New[T any](rdb *redis.Client, opts ...Option) *Handler[T] {
	config := handlerConfig{
		rdb:                         rdb,
		prefix:                      "",
		defaultTTL:                  5 * time.Minute,
		bgRefreshTimeout:            5 * time.Second,
		refreshCooldown:             0,
		defaultMissPolicy:           MissPolicySyncWriteThenReturn,
		staleDataTTL:                24 * time.Hour, // Keep stale data for 24 hours for SWR
		defaultRefreshAheadThreshold: 0.2,           // Refresh when 20% TTL remaining
		defaultProbabilisticBeta:    1.0,            // Standard probabilistic refresh
		cooperativeTimeout:          10 * time.Second, // Max wait for cooperative refresh
	}
	for _, o := range opts {
		o(&config)
	}
	return &Handler[T]{
		config:           config,
		localLocks:       newKeyedMutex(),
		lastRefreshByKey: make(map[string]time.Time),
	}
}

func WithPrefix(prefix string) Option {
	return func(c *handlerConfig) { c.prefix = prefix }
}

func WithDefaultTTL(ttl time.Duration) Option {
	return func(c *handlerConfig) { c.defaultTTL = ttl }
}

// WithBackgroundRefreshTimeout defines how long the background refresh is allowed to run.
func WithBackgroundRefreshTimeout(d time.Duration) Option {
	return func(c *handlerConfig) { c.bgRefreshTimeout = d }
}

// WithRefreshCooldown sets a minimum interval between background refreshes for the same key (hit-path only).
func WithRefreshCooldown(d time.Duration) Option {
	return func(c *handlerConfig) { c.refreshCooldown = d }
}

// WithMissPolicy sets the default miss behavior for this handler.
func WithMissPolicy(p MissPolicy) Option {
	return func(c *handlerConfig) { c.defaultMissPolicy = p }
}

// WithStaleDataTTL sets how long stale data is kept for stale-while-revalidate policy.
func WithStaleDataTTL(ttl time.Duration) Option {
	return func(c *handlerConfig) { c.staleDataTTL = ttl }
}

// WithRefreshAheadThreshold sets the default threshold for refresh-ahead policy.
// Value should be between 0.0 and 1.0 (e.g., 0.2 = refresh when 20% TTL remaining).
func WithRefreshAheadThreshold(threshold float64) Option {
	return func(c *handlerConfig) { 
		if threshold >= 0.0 && threshold <= 1.0 {
			c.defaultRefreshAheadThreshold = threshold 
		}
	}
}

// WithProbabilisticBeta sets the beta parameter for probabilistic refresh policy.
func WithProbabilisticBeta(beta float64) Option {
	return func(c *handlerConfig) { 
		if beta > 0 {
			c.defaultProbabilisticBeta = beta 
		}
	}
}

// WithCooperativeTimeout sets the maximum time to wait for cooperative refresh.
func WithCooperativeTimeout(timeout time.Duration) Option {
	return func(c *handlerConfig) { c.cooperativeTimeout = timeout }
}

func WithTTL(ttl time.Duration) CallOption {
	return func(c *callOpts) { c.ttl = ttl }
}

func WithoutBackgroundRefresh() CallOption {
	return func(c *callOpts) { c.disableHitRefresh = true }
}

func WithCallMissPolicy(p MissPolicy) CallOption {
	return func(c *callOpts) { c.overrideMissPolicy = &p }
}

// WithRefreshAheadThreshold sets the refresh-ahead threshold for this call.
func WithRefreshAheadThreshold(threshold float64) CallOption {
	return func(c *callOpts) { 
		if threshold >= 0.0 && threshold <= 1.0 {
			c.refreshAheadThreshold = threshold 
		}
	}
}

// WithProbabilisticBeta sets the probabilistic refresh beta for this call.
func WithProbabilisticBeta(beta float64) CallOption {
	return func(c *callOpts) { 
		if beta > 0 {
			c.probabilisticRefreshBeta = beta 
		}
	}
}

// WithStaleCheckTimeout sets timeout for checking stale data.
func WithStaleCheckTimeout(timeout time.Duration) CallOption {
	return func(c *callOpts) { c.staleCheckTimeout = timeout }
}

// ---------------------------
// Basic Ops
// ---------------------------

func (h *Handler[T]) fullKey(key string) string {
	if h.config.prefix == "" {
		return key
	}
	return h.config.prefix + ":" + key
}

// Set writes a value with TTL.
func (h *Handler[T]) Set(ctx context.Context, key string, value T, opts ...CallOption) error {
	var co callOpts
	for _, o := range opts {
		o(&co)
	}

	ttl := co.ttl
	if ttl <= 0 {
		ttl = h.config.defaultTTL
	}

	k := h.fullKey(key)
	b, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	if err := h.config.rdb.Set(ctx, k, b, ttl).Err(); err != nil {
		return fmt.Errorf("redis set: %w", err)
	}
	h.setLastRefreshNow(k) // For cooldown accounting
	return nil
}

// Get fetches a value from Redis into T.
func (h *Handler[T]) Get(ctx context.Context, key string) (Result[T], error) {
	var zero T
	k := h.fullKey(key)
	cmd := h.config.rdb.Get(ctx, k)
	if err := cmd.Err(); err != nil {
		if errors.Is(err, redis.Nil) {
			return Result[T]{Value: zero, FromCache: false}, redis.Nil
		}
		return Result[T]{Value: zero}, fmt.Errorf("redis get: %w", err)
	}

	raw, err := cmd.Bytes()
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("bytes: %w", err)
	}
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("unmarshal: %w", err)
	}

	return Result[T]{Value: v, FromCache: true, CachedAt: time.Now()}, nil
}

// ---------------------------
// Main Entry: GetOrRefresh
// ---------------------------

func (h *Handler[T]) GetOrRefresh(ctx context.Context, key string, gen Generator[T], opts ...CallOption) (Result[T], error) {
	var co callOpts
	for _, o := range opts {
		o(&co)
	}
	ttl := co.ttl
	if ttl <= 0 {
		ttl = h.config.defaultTTL
	}
	missPolicy := h.config.defaultMissPolicy
	if co.overrideMissPolicy != nil {
		missPolicy = *co.overrideMissPolicy
	}

	// 1) Try cache
	if res, err := h.Get(ctx, key); err == nil {
		// Handle hit-based refresh policies
		if !co.disableHitRefresh {
			h.handleHitRefresh(ctx, key, ttl, gen, missPolicy, co)
		}
		return res, nil
	} else if !errors.Is(err, redis.Nil) {
		var zero T
		return Result[T]{Value: zero}, err
	}

	// 2) MISS: choose behavior
	switch missPolicy {
	case MissPolicySyncWriteThenReturn:
		return h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	case MissPolicyReturnThenAsyncWrite:
		return h.missReturnThenAsyncWrite(ctx, key, ttl, gen)
	case MissPolicyStaleWhileRevalidate:
		return h.missStaleWhileRevalidate(ctx, key, ttl, gen, co)
	case MissPolicyFailFast:
		return h.missFailFast(ctx, key)
	case MissPolicyRefreshAhead:
		return h.missRefreshAhead(ctx, key, ttl, gen, co)
	case MissPolicyCooperativeRefresh:
		return h.missCooperativeRefresh(ctx, key, ttl, gen)
	case MissPolicyBestEffort:
		return h.missBestEffort(ctx, key, ttl, gen)
	case MissPolicyProbabilisticRefresh:
		return h.missProbabilisticRefresh(ctx, key, ttl, gen, co)
	default:
		var zero T
		return Result[T]{Value: zero}, fmt.Errorf("unknown miss policy: %v", missPolicy)
	}
}
