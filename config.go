package cache

import (
	"time"

	"github.com/redis/go-redis/v9"
)

// handlerConfig holds non-generic configuration fields.
type handlerConfig struct {
	rdb                      *redis.Client
	prefix                   string
	defaultTTL               time.Duration
	bgRefreshTimeout         time.Duration
	refreshCooldown          time.Duration // Min gap between background refreshes for the same key after HIT
	defaultMissPolicy        MissPolicy
	staleDataTTL             time.Duration // How long to keep stale data for SWR policy
	defaultRefreshAheadThreshold float64   // Default threshold for refresh-ahead policy (0.2 = 20%)
	defaultProbabilisticBeta float64       // Default beta for probabilistic refresh (1.0)
	cooperativeTimeout       time.Duration // Max time to wait for cooperative refresh
}
