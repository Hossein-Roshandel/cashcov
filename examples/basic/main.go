package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"time"

	"cache"

	"github.com/redis/go-redis/v9"
)

// Configuration constants for the cache.
const (
	TTLminutes             = 1 // Cache TTL set to 1 minute
	RefreshCoolDownSeconds = 6 // Cooldown period for refresh in seconds
)

// dataSource simulates an external data source (e.g., database or API) that provides fresh data.
func dataSource(_ context.Context, _ string) (string, error) {
	// Simulate fetching fresh data with a random suffix to show when data is refreshed
	return fmt.Sprintf("user_data_%d_%s", rand.IntN(1000), time.Now().Format("15:04:05")), nil
}

// fetchDirectFromRedis retrieves and unmarshals a string value directly from Redis.
func fetchDirectFromRedis(ctx context.Context, rdb *redis.Client, key string) (string, error) {
	redisResult, err := rdb.Get(ctx, key).Bytes()
	if err != nil {
		return "", fmt.Errorf("failed to get value from Redis: %w", err)
	}
	var redisValue string
	err = json.Unmarshal(redisResult, &redisValue)
	if err != nil {
		return "", fmt.Errorf("failed to unmarshal Redis value: %w", err)
	}
	return redisValue, nil
}

func main() {
	var err error
	var handler *cache.Handler[string]
	var result cache.Result[string]
	var redisValue string

	// Initialize Redis client
	rdb := redis.NewClient(&redis.Options{
		Addr: "redis:6379", // Redis server address
	})

	// Create a type-safe cache handler for strings with specific configurations
	handler, err = cache.New[string](rdb,
		cache.WithPrefix("myapp"),                                     // Prefix for Redis keys
		cache.WithDefaultTTL(TTLminutes*time.Minute),                  // TTL for cache entries
		cache.WithRefreshCooldown(RefreshCoolDownSeconds*time.Second), // Cooldown for refresh
		cache.WithMissPolicy(cache.MissPolicyReturnThenAsyncWrite),    // Policy for cache misses
	)
	if err != nil {
		panic(fmt.Sprintf("Failed to create cache handler: %v", err))
	}

	ctx := context.Background()

	// Set an initial value in the cache
	key := "user:123"
	initialValue := "initial_user_data"
	err = handler.Set(ctx, key, initialValue)
	if err != nil {
		panic(fmt.Sprintf("Failed to set initial value: %v", err))
	}
	fmt.Printf("Initial value set for key '%s': %s\n\n", key, initialValue)

	// Run a loop to demonstrate cache behavior over time
	fmt.Println("Starting cache behavior demonstration...")
	fmt.Println("Key: myapp:user:123")
	fmt.Println("TTL: 1 minute, Refresh Cooldown: 3 seconds")
	fmt.Println("------------------------------------------------")

	for i := range 10 {
		// Get or refresh value using the wrapper
		result, err = handler.GetOrRefresh(ctx, key, func(ctx context.Context) (string, error) {
			return dataSource(ctx, key)
		})
		if err != nil {
			panic(fmt.Sprintf("Failed to get/refresh value: %v", err))
		}

		time.Sleep(1 * time.Second)

		// Fetch the raw value directly from Redis for comparison
		redisValue, err = fetchDirectFromRedis(ctx, rdb, "myapp:"+key)
		if err != nil {
			panic(err)
		}

		// Print results to compare wrapper's result with direct Redis result
		fmt.Printf("Iteration %d (after %d seconds):\n", i+1, i*4)
		fmt.Printf("  Wrapper Value: %s (From Cache: %t)\n", result.Value, result.FromCache)
		fmt.Printf("  Direct Redis Value: %s\n", redisValue)
		if result.Value == redisValue {
			fmt.Println("  Status: Values match (data is fresh or from cache)")
		} else {
			fmt.Println("  Status: Values differ (wrapper may have refreshed data or used cache)")
		}
		fmt.Println("------------------------------------------------")

		// Wait to simulate time passing and observe TTL/cooldown effects
		time.Sleep(4 * time.Second)
	}
}
