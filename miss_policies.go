package cache

// MissPolicy controls the cache miss behavior.
type MissPolicy int

const (
	// MissPolicySyncWriteThenReturn On miss, acquire lock, double-check,
	// generate, write to Redis, then return the generated value.
	MissPolicySyncWriteThenReturn MissPolicy = iota

	// MissPolicyReturnThenAsyncWrite On miss, generate immediately and return,
	// then in background acquire lock, double-check, and write if still missing.
	MissPolicyReturnThenAsyncWrite

	// MissPolicyStaleWhileRevalidate On miss, check for expired data. If found,
	// return it immediately while refreshing in background. If no stale data,
	// generate synchronously.
	MissPolicyStaleWhileRevalidate

	// MissPolicyFailFast On miss, return immediately with a specific error
	// without attempting to generate data. Useful for fail-fast scenarios.
	MissPolicyFailFast

	// MissPolicyRefreshAhead Proactively refresh cache when TTL drops below
	// a threshold (e.g., 20% remaining). On miss, behaves like sync write.
	MissPolicyRefreshAhead

	// MissPolicyCooperativeRefresh On miss, first request acquires lock and
	// generates data while other concurrent requests wait for the result.
	// Similar to sync but optimized for high concurrency scenarios.
	MissPolicyCooperativeRefresh

	// MissPolicyBestEffort On miss, try to generate data. If generation fails,
	// return a zero value instead of an error (graceful degradation).
	MissPolicyBestEffort

	// MissPolicyProbabilisticRefresh Uses probabilistic early expiration to
	// distribute refresh load. Probability increases as data ages.
	MissPolicyProbabilisticRefresh
)
