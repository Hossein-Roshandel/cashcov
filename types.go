package cache

import (
	"context"
	"errors"
	"time"
)

// Result wraps the returned value and some metadata.
type Result[T any] struct {
	Value     T
	FromCache bool
	CachedAt  time.Time // Best-effort: time when we SET into Redis or when we fetched
}

// Generator is the function that produces fresh data.
type Generator[T any] func(ctx context.Context) (T, error)

// Option configures the handlerConfig.
type Option func(*handlerConfig)

// CallOption configures a single call.
type CallOption func(*callOpts)

// ErrCacheMiss is returned when MissFillFailFast is active and the key is not in the cache.
var ErrCacheMiss = errors.New("cache miss")

type callOpts struct {
	ttl                      time.Duration
	disableHitRefresh        bool
	overrideMissFillPolicy   *MissFillPolicy
	overrideHitRefreshPolicy *HitRefreshPolicy
	overrideErrorPolicy      *ErrorPolicy
	refreshAheadThreshold    float64       // Percentage of TTL remaining to trigger refresh-ahead (0.0-1.0)
	probabilisticRefreshBeta float64       // Beta parameter for probabilistic refresh (default: 1.0)
	refreshOlderThanAge      time.Duration // Age threshold for HitRefreshOlderThan
	staleCheckTimeout        time.Duration // Timeout for checking stale data
}
