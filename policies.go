package cache

// MissFillPolicy controls what happens when data is not found in the cache.
// It is one of three independent cache behaviour axes; see also HitRefreshPolicy
// and ErrorPolicy.
type MissFillPolicy int

const (
	// MissFillDefault is the zero value; the handler falls back to MissFillSync.
	MissFillDefault MissFillPolicy = iota

	// MissFillSync acquires a per-key in-process lock, double-checks the cache,
	// generates the value, writes it, then returns. Prevents cache stampede.
	// Highest consistency; higher latency during a miss.
	MissFillSync

	// MissFillAsync generates the value and returns it immediately, then writes to
	// the cache in the background. Lowest miss latency; multiple concurrent callers
	// may all invoke the generator simultaneously (no stampede protection on the
	// first miss wave). Use WithMissDeduplicationWindow to suppress duplicate
	// generation after the first write within a configurable time window.
	MissFillAsync

	// MissFillStaleOrSync returns stale (expired) data immediately if available,
	// triggering a background refresh. Falls back to MissFillSync when no stale
	// data exists. Requires WithStaleDataTTL on the handler; without it the stale
	// lookup will always miss and this behaves identically to MissFillSync.
	MissFillStaleOrSync

	// MissFillFailFast returns ErrCacheMiss immediately without calling the
	// generator. Intended for use with a circuit-breaker or an explicit fallback.
	MissFillFailFast

	// MissFillCooperative allows the first concurrent request to acquire an
	// in-process lock and generate the value; all other requests for the same key
	// block until the lock is released or WithCooperativeTimeout elapses, at which
	// point they fall back to direct generation without caching.
	MissFillCooperative
)

// HitRefreshPolicy controls proactive background refresh behaviour when the
// requested key is found in the cache. It is independent of MissFillPolicy.
type HitRefreshPolicy int

const (
	// HitRefreshDefault is the zero value; the handler performs a standard
	// background refresh on every hit, gated by the configured refresh cooldown.
	HitRefreshDefault HitRefreshPolicy = iota

	// HitRefreshAhead triggers a background refresh when the remaining TTL of the
	// cached entry drops below a configurable fraction of the original TTL (e.g.
	// 20%). Configure the threshold with WithRefreshAheadThreshold.
	HitRefreshAhead

	// HitRefreshProbabilistic uses the XFetch algorithm: the probability of an
	// early refresh increases continuously as the entry ages, distributing refresh
	// load across requests without coordination. Configure sensitivity with
	// WithProbabilisticBeta.
	HitRefreshProbabilistic

	// HitRefreshOlderThan triggers a background refresh when the age of the cached
	// entry exceeds a configurable duration (e.g. 10 minutes). Age is estimated as
	// originalTTL minus the remaining Redis TTL. Configure the threshold with
	// WithRefreshOlderThanAge. The background refresh is stampede-protected: a
	// per-key TryLock ensures only one goroutine in this process runs the generator
	// at a time, and the configured refresh cooldown prevents back-to-back writes.
	HitRefreshOlderThan

	// HitRefreshNone disables all background refresh on cache hits.
	HitRefreshNone
)

// ErrorPolicy controls how a generator error is surfaced to the caller.
// It is independent of MissFillPolicy and HitRefreshPolicy.
type ErrorPolicy int

const (
	// ErrorPolicySurface returns the generator error to the caller. This is the
	// default and the zero value.
	ErrorPolicySurface ErrorPolicy = iota

	// ErrorPolicyZeroValue suppresses generator errors: the caller receives a
	// zero-valued result with a nil error. ErrCacheMiss (from MissFillFailFast) is
	// never suppressed — that is an intentional signal, not a generation failure.
	// Use for non-critical data where partial availability is acceptable.
	ErrorPolicyZeroValue
)
