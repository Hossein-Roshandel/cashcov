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

// ErrCacheMissFailFast is returned when MissPolicyFailFast is used and cache miss occurs.
var ErrCacheMissFailFast = errors.New("cache miss with fail-fast policy")

type callOpts struct {
	ttl                      time.Duration
	disableHitRefresh        bool
	overrideMissPolicy       *MissPolicy
	refreshAheadThreshold    float64       // Percentage of TTL remaining to trigger refresh-ahead (0.0-1.0)
	probabilisticRefreshBeta float64       // Beta parameter for probabilistic refresh (default: 1.0)
	staleCheckTimeout        time.Duration // Timeout for checking stale data
}

// FallbackGenerator is a function that provides fallback data for best-effort policy.
type FallbackGenerator[T any] func(ctx context.Context) (T, error)
