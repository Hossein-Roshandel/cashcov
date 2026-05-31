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
	localLocks       *KeyedMutex
	lastRefreshByKey map[string]time.Time
	lastRefreshMu    sync.Mutex
}

// New creates a new cache Handler[T].
func New[T any](rdb *redis.Client, opts ...Option) (*Handler[T], error) {
	config, err := loadHandlerConfig(rdb)
	if err != nil {
		return nil, err
	}

	for _, o := range opts {
		o(config)
	}
	return &Handler[T]{
		config:           *config,
		localLocks:       NewKeyedMutex(),
		lastRefreshByKey: make(map[string]time.Time),
	}, nil
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

// WithMissFillPolicy sets the default miss-fill behaviour for this handler.
func WithMissFillPolicy(p MissFillPolicy) Option {
	return func(c *handlerConfig) { c.defaultMissFillPolicy = p }
}

// WithDefaultHitRefreshPolicy sets the default hit-refresh behaviour for this handler.
func WithDefaultHitRefreshPolicy(p HitRefreshPolicy) Option {
	return func(c *handlerConfig) { c.defaultHitRefreshPolicy = p }
}

// WithDefaultErrorPolicy sets the default error-handling behaviour for this handler.
func WithDefaultErrorPolicy(p ErrorPolicy) Option {
	return func(c *handlerConfig) { c.defaultErrorPolicy = p }
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

// WithRefreshOlderThanAge sets the default age threshold for the HitRefreshOlderThan
// policy. A background refresh is triggered when a cached entry's age (originalTTL
// minus remaining Redis TTL) exceeds d.
func WithRefreshOlderThanAge(d time.Duration) Option {
	return func(c *handlerConfig) {
		if d > 0 {
			c.defaultRefreshOlderThanAge = d
		}
	}
}

// WithCallRefreshOlderThanAge overrides the age threshold for HitRefreshOlderThan
// for a single call.
func WithCallRefreshOlderThanAge(d time.Duration) CallOption {
	return func(c *callOpts) { c.refreshOlderThanAge = d }
}

// WithCooperativeTimeout sets the maximum time to wait for cooperative refresh.
func WithCooperativeTimeout(timeout time.Duration) Option {
	return func(c *handlerConfig) { c.cooperativeTimeout = timeout }
}

// WithMissDeduplicationWindow sets a minimum interval during which this process
// will not invoke the generator for the same key more than once.
//
// On a cache miss, before calling the generator, the handler checks whether it
// wrote this key within the window. If so, it retries the Redis GET once — the
// key may have just been written by a concurrent goroutine. If the key is still
// absent (e.g. evicted or TTL already elapsed again), the generator is called
// normally.
//
// The effective window is clamped to the call's TTL at runtime. Setting it
// longer than the TTL is a no-op: the key cannot exist in Redis beyond its TTL,
// so any window exceeding it adds no protection.
//
// This is an in-process guard only; it does not coordinate across pods.
// It is most effective with MissFillAsync, where multiple goroutines otherwise
// all invoke the generator simultaneously on the same miss wave.
func WithMissDeduplicationWindow(d time.Duration) Option {
	return func(c *handlerConfig) {
		if d > 0 {
			c.missDeduplicationWindow = d
		}
	}
}

func WithTTL(ttl time.Duration) CallOption {
	return func(c *callOpts) { c.ttl = ttl }
}

func WithoutBackgroundRefresh() CallOption {
	return func(c *callOpts) { c.disableHitRefresh = true }
}

func WithCallMissFillPolicy(p MissFillPolicy) CallOption {
	return func(c *callOpts) { c.overrideMissFillPolicy = &p }
}

// WithCallHitRefreshPolicy overrides the hit-refresh policy for a single call.
func WithCallHitRefreshPolicy(p HitRefreshPolicy) CallOption {
	return func(c *callOpts) { c.overrideHitRefreshPolicy = &p }
}

// WithCallErrorPolicy overrides the error policy for a single call.
func WithCallErrorPolicy(p ErrorPolicy) CallOption {
	return func(c *callOpts) { c.overrideErrorPolicy = &p }
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

	var err error
	var b []byte

	k := h.fullKey(key)
	b, err = json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	if err = h.config.rdb.Set(ctx, k, b, ttl).Err(); err != nil {
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
	var err error
	var raw []byte

	raw, err = cmd.Bytes()
	if err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("bytes: %w", err)
	}
	var v T
	if err = json.Unmarshal(raw, &v); err != nil {
		return Result[T]{Value: zero}, fmt.Errorf("unmarshal: %w", err)
	}

	return Result[T]{Value: v, FromCache: true, CachedAt: time.Now()}, nil
}

// ---------------------------
// Main Entry: GetOrRefresh
// ---------------------------

func (h *Handler[T]) GetOrRefresh(
	ctx context.Context,
	key string,
	gen Generator[T],
	opts ...CallOption,
) (Result[T], error) {
	var co callOpts
	for _, o := range opts {
		o(&co)
	}
	ttl := co.ttl
	if ttl <= 0 {
		ttl = h.config.defaultTTL
	}

	missFill := h.config.defaultMissFillPolicy
	if co.overrideMissFillPolicy != nil {
		missFill = *co.overrideMissFillPolicy
	}
	if missFill == MissFillDefault {
		missFill = MissFillSync
	}

	hitRefresh := h.config.defaultHitRefreshPolicy
	if co.overrideHitRefreshPolicy != nil {
		hitRefresh = *co.overrideHitRefreshPolicy
	}

	errPolicy := h.config.defaultErrorPolicy
	if co.overrideErrorPolicy != nil {
		errPolicy = *co.overrideErrorPolicy
	}

	var res Result[T]
	var err error

	// 1) Try cache
	if res, err = h.Get(ctx, key); err == nil {
		// Handle hit-based refresh policies
		if !co.disableHitRefresh {
			h.handleHitRefresh(ctx, key, ttl, gen, hitRefresh, co)
		}
		return res, nil
	} else if !errors.Is(err, redis.Nil) {
		var zero T
		return Result[T]{Value: zero}, err
	}

	// 2) MISS: in-process deduplication pre-flight.
	// If this process wrote the key within missDeduplicationWindow, retry the
	// Redis GET before calling the generator. See checkDeduplicationBypass for details.
	if retryRes, bypassed := h.checkDeduplicationBypass(ctx, key, ttl); bypassed {
		return retryRes, nil
	}

	// 3) Dispatch on fill policy
	switch missFill { //nolint:exhaustive // MissFillDefault is normalised to MissFillSync above
	case MissFillSync:
		res, err = h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	case MissFillAsync:
		res, err = h.missReturnThenAsyncWrite(ctx, key, ttl, gen)
	case MissFillStaleOrSync:
		res, err = h.missStaleWhileRevalidate(ctx, key, ttl, gen, co)
	case MissFillFailFast:
		res, err = h.missFailFast(ctx, key)
	case MissFillCooperative:
		res, err = h.missCooperativeRefresh(ctx, key, ttl, gen)
	default:
		res, err = h.missSyncWriteThenReturn(ctx, key, ttl, gen)
	}

	// Record creation time for probabilistic refresh after a successful fill
	if err == nil && hitRefresh == HitRefreshProbabilistic {
		fullKey := h.fullKey(key)
		h.lastRefreshMu.Lock()
		h.lastRefreshByKey[fullKey+"@created"] = time.Now()
		h.lastRefreshMu.Unlock()
	}

	// 3) Apply error policy — never suppress ErrCacheMiss (that is an intentional signal)
	if err != nil && errPolicy == ErrorPolicyZeroValue && !errors.Is(err, ErrCacheMiss) {
		var zero T
		return Result[T]{Value: zero, FromCache: false}, nil
	}

	return res, err
}
