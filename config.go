package cache

import (
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/joho/godotenv"
	"github.com/redis/go-redis/v9"
)

// Constants for fallback configuration values.
const (
	// defaultTTLMinutesFallback is the fallback duration in minutes for the default cache TTL.
	defaultTTLMinutesFallback = 5
	// bgRefreshTimeoutSecondsFallback is the fallback duration in seconds for background refresh timeouts.
	bgRefreshTimeoutSecondsFallback = 5
	// staleDataTTLHoursFallback is the fallback duration in hours for stale data TTL.
	staleDataTTLHoursFallback = 24
	// refreshCooldownSecondsFallback is the fallback duration in seconds for the refresh cooldown.
	refreshCooldownSecondsFallback = 0
	// cooperativeTimeoutSecondsFallback is the fallback duration in seconds for cooperative refresh timeouts.
	cooperativeTimeoutSecondsFallback = 10
	// refreshAheadThresholdFallback is the fallback threshold (fraction of TTL) for proactive refresh.
	refreshAheadThresholdFallback = 0.2
	// defaultProbabilisticBetaFallback is the fallback beta value for probabilistic refresh.
	defaultProbabilisticBetaFallback = 1.0
)

// handlerConfig holds non-generic configuration fields.
type handlerConfig struct {
	rdb                          *redis.Client
	prefix                       string
	defaultTTL                   time.Duration
	bgRefreshTimeout             time.Duration
	refreshCooldown              time.Duration // Min gap between background refreshes for the same key after HIT
	defaultMissPolicy            MissPolicy
	staleDataTTL                 time.Duration // How long to keep stale data for SWR policy
	defaultRefreshAheadThreshold float64       // Default threshold for refresh-ahead policy (0.2 = 20%)
	defaultProbabilisticBeta     float64       // Default beta for probabilistic refresh (1.0)
	cooperativeTimeout           time.Duration // Max time to wait for cooperative refresh
}

// parseEnvDuration parses an environment variable as a float64 and converts it to a time.Duration with the given unit.
// It returns the fallback value if the variable is missing, invalid, or non-positive (unless allowZero is true).
func parseEnvDuration(envKey string, unit time.Duration, fallback float64, allowZero bool) (time.Duration, error) {
	valueStr := os.Getenv(envKey)
	if valueStr == "" {
		return time.Duration(fallback) * unit, nil
	}
	value, err := strconv.ParseFloat(valueStr, 64)
	if err != nil {
		return time.Duration(fallback) * unit, fmt.Errorf("parse %s: %w", envKey, err)
	}
	if value < 0 || (!allowZero && value == 0) {
		return time.Duration(
				fallback,
			) * unit, fmt.Errorf(
				"%s must be %s0",
				envKey,
				map[bool]string{true: "", false: "> "}[allowZero],
			)
	}
	return time.Duration(value) * unit, nil
}

// parseEnvFloat parses an environment variable as a float64, returning the fallback if missing, invalid, or non-positive (unless allowZero is true).
func parseEnvFloat(envKey string, fallback float64, allowZero bool) (float64, error) {
	valueStr := os.Getenv(envKey)
	if valueStr == "" {
		return fallback, nil
	}
	value, err := strconv.ParseFloat(valueStr, 64)
	if err != nil {
		return fallback, fmt.Errorf("parse %s: %w", envKey, err)
	}
	if value < 0 || (!allowZero && value == 0) {
		return fallback, fmt.Errorf("%s must be %s0", envKey, map[bool]string{true: "", false: "> "}[allowZero])
	}
	return value, nil
}

// loadHandlerConfig loads cache configuration from environment variables with fallback defaults.
// It returns a configured handlerConfig or an error if the .env file or environment variables cannot be processed.
//
// Parameters:
//   - rdb: The Redis client to use for cache operations.
//
// Returns:
//   - *handlerConfig: The populated configuration.
//   - error: Any error from loading the .env file or parsing environment variables.
func loadHandlerConfig(rdb *redis.Client) (*handlerConfig, error) {
	// Load .env file
	if err := godotenv.Load(); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("failed to load .env file: %w", err)
	}

	// Parse environment variables with defaults
	defaultTTL, err := parseEnvDuration(
		"CACHE_DEFAULT_TTL_MINUTES",
		time.Minute,
		defaultTTLMinutesFallback,
		false,
	)
	if err != nil {
		return nil, err
	}
	bgRefreshTimeout, err := parseEnvDuration(
		"CACHE_BG_REFRESH_TIMEOUT_SECONDS",
		time.Second,
		bgRefreshTimeoutSecondsFallback,
		false,
	)
	if err != nil {
		return nil, err
	}
	staleDataTTL, err := parseEnvDuration(
		"CACHE_STALE_DATA_TTL_HOURS",
		time.Hour,
		staleDataTTLHoursFallback,
		false,
	)
	if err != nil {
		return nil, err
	}

	refreshCooldown, err := parseEnvDuration(
		"CACHE_BG_REFRESH_COOLDOWN_SECONDS",
		time.Second,
		refreshCooldownSecondsFallback,
		true,
	)
	if err != nil {
		return nil, err
	}

	cooperativeTimeout, err := parseEnvDuration(
		"CACHE_COOPERATIVE_TIMEOUT_SECONDS",
		time.Second,
		cooperativeTimeoutSecondsFallback,
		false,
	)
	if err != nil {
		return nil, err
	}

	refreshAheadThreshold, err := parseEnvFloat(
		"CACHE_REFRESH_AHEAD_THRESHOLD",
		refreshAheadThresholdFallback,
		false,
	)
	if err != nil {
		return nil, err
	}

	defaultProbabilisticBeta, err := parseEnvFloat(
		"CACHE_DEFAULT_PROBABILISTIC_BETA",
		defaultProbabilisticBetaFallback,
		false,
	)
	if err != nil {
		return nil, err
	}

	config := handlerConfig{
		rdb:                          rdb,
		prefix:                       "", // Could also be an env var if needed
		defaultTTL:                   defaultTTL,
		bgRefreshTimeout:             bgRefreshTimeout,
		refreshCooldown:              refreshCooldown,
		defaultMissPolicy:            MissPolicySyncWriteThenReturn,
		staleDataTTL:                 staleDataTTL,
		defaultRefreshAheadThreshold: refreshAheadThreshold,
		defaultProbabilisticBeta:     defaultProbabilisticBeta,
		cooperativeTimeout:           cooperativeTimeout,
	}
	return &config, nil
}
