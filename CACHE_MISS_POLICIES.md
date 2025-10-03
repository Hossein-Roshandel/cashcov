# Cache Miss Policies - Comprehensive Guide

This document provides detailed information about all cache miss policies available in the Redis Cache Wrapper.

## Overview

Cache miss policies determine how the system behaves when requested data is not found in the cache. Each policy is optimized for different use cases and performance characteristics.

## Policy Comparison Matrix

| Policy | Response Time | Consistency | Cache Stampede Prevention | Resource Usage | Best Use Case |
|--------|---------------|-------------|---------------------------|----------------|---------------|
| **SyncWriteThenReturn** | Slower | Strong | Excellent | Low | Critical data consistency |
| **ReturnThenAsyncWrite** | Faster | Eventual | Poor | Medium | High-performance APIs |
| **StaleWhileRevalidate** | Fastest | Eventual | Good | Medium | Content delivery, web apps |
| **FailFast** | Fastest | N/A | N/A | Lowest | Circuit breaker pattern |
| **RefreshAhead** | Variable | Strong | Excellent | Medium | Predictable workloads |
| **CooperativeRefresh** | Medium | Strong | Excellent | High | High concurrency |
| **BestEffort** | Medium | Weak | Good | Low | Graceful degradation |
| **ProbabilisticRefresh** | Variable | Eventual | Good | Medium | Distributed load |

## Detailed Policy Descriptions

### 1. MissPolicySyncWriteThenReturn (Default)
**Behavior**: Synchronous generation with locking
- On cache miss: acquire lock → double-check → generate → write to cache → return value
- **Pros**: Strong consistency, prevents cache stampede, predictable behavior
- **Cons**: Higher latency during miss, can create bottlenecks
- **Configuration**: No additional config needed

```go
handler := cache.New[string](rdb, 
    cache.WithMissPolicy(cache.MissPolicySyncWriteThenReturn))

result, err := handler.GetOrRefresh(ctx, "key", generator)
```

### 2. MissPolicyReturnThenAsyncWrite
**Behavior**: Immediate return with background cache write
- On cache miss: generate immediately → return value → write to cache in background
- **Pros**: Low latency, fast response times
- **Cons**: Potential cache stampede, eventual consistency
- **Configuration**: Uses `bgRefreshTimeout` for background write timeout

```go
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyReturnThenAsyncWrite))
```

### 3. MissPolicyStaleWhileRevalidate
**Behavior**: Return stale data while refreshing in background
- On cache miss: check for stale data → if found, return immediately + refresh in background → if not found, generate synchronously
- **Pros**: Ultra-low latency for subsequent misses, high availability
- **Cons**: May return outdated data, requires additional storage
- **Configuration**: Use `WithStaleDataTTL()` to control stale data retention

```go
handler := cache.New[string](rdb,
    cache.WithStaleDataTTL(24*time.Hour)) // Keep stale data for 24 hours

result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyStaleWhileRevalidate),
    cache.WithStaleCheckTimeout(500*time.Millisecond))
```

### 4. MissPolicyFailFast
**Behavior**: Immediate error return without data generation
- On cache miss: return `ErrCacheMissFailFast` error immediately
- **Pros**: Predictable latency, perfect for circuit breaker patterns
- **Cons**: No automatic data generation, requires external handling
- **Configuration**: No additional config needed

```go
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyFailFast))

if errors.Is(err, cache.ErrCacheMissFailFast) {
    // Handle cache miss explicitly
    fallbackValue := getFallbackData()
}
```

### 5. MissPolicyRefreshAhead
**Behavior**: Proactive refresh before expiration
- On cache miss: generate synchronously (like sync policy)
- On cache hit: if TTL < threshold, refresh in background
- **Pros**: Prevents cache misses, consistent performance
- **Cons**: Increased background activity, may refresh unused data
- **Configuration**: Use `WithRefreshAheadThreshold()` to set refresh trigger point

```go
handler := cache.New[string](rdb,
    cache.WithRefreshAheadThreshold(0.2)) // Refresh when 20% TTL remaining

result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyRefreshAhead),
    cache.WithRefreshAheadThreshold(0.3)) // Override to 30% for this call
```

### 6. MissPolicyCooperativeRefresh
**Behavior**: Concurrent requests wait for the first one to complete
- On cache miss: first request generates data, others wait for result
- **Pros**: Excellent cache stampede prevention, efficient resource usage
- **Cons**: Potential timeout issues, complexity in high-concurrency scenarios
- **Configuration**: Use `WithCooperativeTimeout()` to set max wait time

```go
handler := cache.New[string](rdb,
    cache.WithCooperativeTimeout(5*time.Second)) // Max 5s wait

result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyCooperativeRefresh))
```

### 7. MissPolicyBestEffort
**Behavior**: Graceful degradation on generator failure
- On cache miss: attempt generation → if success, cache and return → if failure, return zero value (no error)
- **Pros**: High availability, graceful degradation
- **Cons**: May return empty/default values, masks generation errors
- **Configuration**: No additional config needed

```go
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyBestEffort))

// err will be nil even if generator fails
// result.Value might be zero value if generation failed
```

### 8. MissPolicyProbabilisticRefresh
**Behavior**: Random early refresh based on age
- On cache miss: generate synchronously
- On cache hit: probabilistic refresh based on data age and beta parameter
- **Pros**: Distributes refresh load, prevents thundering herd
- **Cons**: Non-deterministic behavior, requires tuning
- **Configuration**: Use `WithProbabilisticBeta()` to control refresh probability

```go
handler := cache.New[string](rdb,
    cache.WithProbabilisticBeta(1.0)) // Standard beta value

result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissPolicy(cache.MissPolicyProbabilisticRefresh),
    cache.WithProbabilisticBeta(2.0)) // More aggressive refresh
```

## Configuration Options

### Handler-Level Configuration
```go
handler := cache.New[string](rdb,
    // Basic options
    cache.WithPrefix("myapp"),
    cache.WithDefaultTTL(5*time.Minute),
    cache.WithMissPolicy(cache.MissPolicyStaleWhileRevalidate),
    
    // Advanced options
    cache.WithStaleDataTTL(24*time.Hour),
    cache.WithRefreshAheadThreshold(0.2),
    cache.WithProbabilisticBeta(1.0),
    cache.WithCooperativeTimeout(10*time.Second),
)
```

### Call-Level Configuration
```go
result, err := handler.GetOrRefresh(ctx, "key", generator,
    // Override handler defaults for this call
    cache.WithTTL(30*time.Minute),
    cache.WithCallMissPolicy(cache.MissPolicyRefreshAhead),
    cache.WithRefreshAheadThreshold(0.3),
    cache.WithStaleCheckTimeout(1*time.Second),
)
```

## Use Case Recommendations

### Web Applications
- **Primary**: `MissPolicyStaleWhileRevalidate` for content
- **Secondary**: `MissPolicyRefreshAhead` for user data
- **Fallback**: `MissPolicyBestEffort` for non-critical features

### High-Frequency APIs
- **Primary**: `MissPolicyReturnThenAsyncWrite` for speed
- **Secondary**: `MissPolicyProbabilisticRefresh` for load distribution
- **Circuit Breaker**: `MissPolicyFailFast` for overload protection

### Database-Backed Services
- **Primary**: `MissPolicySyncWriteThenReturn` for consistency
- **Secondary**: `MissPolicyCooperativeRefresh` for expensive queries
- **Background**: `MissPolicyRefreshAhead` for predictable loads

### Real-Time Systems
- **Primary**: `MissPolicyFailFast` for predictable latency
- **Fallback**: `MissPolicyBestEffort` for graceful degradation
- **Monitoring**: `MissPolicyStaleWhileRevalidate` for dashboards

### Content Delivery
- **Primary**: `MissPolicyStaleWhileRevalidate` for assets
- **Secondary**: `MissPolicyProbabilisticRefresh` for popular content
- **Edge**: `MissPolicyRefreshAhead` for trending content

## Performance Tuning

### Low Latency Requirements
```go
handler := cache.New[T](rdb,
    cache.WithMissPolicy(cache.MissPolicyReturnThenAsyncWrite),
    cache.WithDefaultTTL(2*time.Minute),
    cache.WithBackgroundRefreshTimeout(1*time.Second),
)
```

### High Consistency Requirements
```go
handler := cache.New[T](rdb,
    cache.WithMissPolicy(cache.MissPolicySyncWriteThenReturn),
    cache.WithDefaultTTL(10*time.Minute),
    cache.WithRefreshCooldown(30*time.Second),
)
```

### High Availability Requirements
```go
handler := cache.New[T](rdb,
    cache.WithMissPolicy(cache.MissPolicyStaleWhileRevalidate),
    cache.WithStaleDataTTL(48*time.Hour),
    cache.WithDefaultTTL(5*time.Minute),
)
```

## Error Handling

Different policies handle errors differently:

```go
result, err := handler.GetOrRefresh(ctx, "key", generator, 
    cache.WithCallMissPolicy(policy))

switch {
case errors.Is(err, cache.ErrCacheMissFailFast):
    // Handle fail-fast policy
    fallbackValue := getDefault()
    
case err != nil && policy == cache.MissPolicyBestEffort:
    // This won't happen - best effort never returns errors
    // Check result.Value for zero value instead
    
case err != nil:
    // Handle generation or Redis errors for other policies
    log.Printf("Cache error: %v", err)
}
```

## Monitoring and Observability

Track policy effectiveness:

```go
// Monitor hit rates by policy
func (h *Handler[T]) GetStats() Stats {
    return Stats{
        Hits:   h.hitCount.Load(),
        Misses: h.missCount.Load(),
        Errors: h.errorCount.Load(),
        // ... other metrics
    }
}

// Custom metrics for specific policies
func trackPolicyMetrics(policy MissPolicy, duration time.Duration, fromCache bool) {
    switch policy {
    case MissPolicyStaleWhileRevalidate:
        if fromCache {
            staleHitCounter.Inc()
        } else {
            staleMissCounter.Inc()
        }
    case MissPolicyFailFast:
        fastFailCounter.Inc()
    // ... other policies
    }
}
```