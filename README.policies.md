# Cache Miss Policies Overview

This document describes the cache miss policies defined in the cache package, their behavior, and their differences. Each policy dictates how the cache handles a miss (when a requested key is not found in the cache) and is designed for specific use cases based on trade-offs between latency, consistency, availability, and performance.

## Cache Miss Policies

Below is a table summarizing the eight cache miss policies, followed by detailed descriptions.

| Policy | Behavior | Latency | Consistency | Use Case | Trade-offs |
|--------|----------|---------|-------------|----------|------------|
| **SyncWriteThenReturn** | Acquires lock, double-checks, generates data, writes to Redis, then returns. | High (synchronous) | High (fresh data guaranteed) | When consistency is critical (e.g., financial data). | Slower due to locking and sync generation. |
| **ReturnThenAsyncWrite** | Generates and returns data immediately, writes to Redis asynchronously. | Low (immediate return) | Medium (cache may lag briefly) | High-traffic APIs needing low latency. | Cache may be outdated until async write completes. |
| **StaleWhileRevalidate** | Returns stale data (if available) and refreshes in background; otherwise, generates synchronously. | Low (if stale data exists) | Medium (may return stale data) | Content delivery where stale data is acceptable. | May serve stale data; sync generation can be slow. |
| **FailFast** | Returns an error immediately without generating data. | Very Low (no generation) | N/A (no data returned) | Strict validation or costly generation scenarios. | No fallback; callers must handle errors. |
| **RefreshAhead** | Proactively refreshes when TTL is low; on miss, behaves like SyncWriteThenReturn. | Medium (proactive refresh reduces misses) | High (fresh data on miss) | Frequently accessed data to minimize misses. | Increased background processing for refreshes. |
| **CooperativeRefresh** | First request generates data; others wait for the result. | Medium (waiting for lock) | High (fresh data for all) | High-concurrency environments with simultaneous misses. | Waiting requests may experience delays. |
| **BestEffort** | Generates data; returns zero value if generation fails. | Medium (depends on generation) | Low (may return zero value) | Non-critical features needing high availability. | May return invalid data; requires careful handling. |
| **ProbabilisticRefresh** | Refreshes probabilistically as TTL nears expiration; on miss, generates synchronously. | Medium (depends on probability) | Medium (may serve stale data) | Large-scale systems to avoid refresh spikes. | Complex to tune; may serve stale data occasionally. |

## Detailed Descriptions

1. **MissPolicySyncWriteThenReturn**:
   - Locks to prevent race conditions, double-checks the cache, generates data, updates Redis, and returns the fresh value.
   - Ideal for scenarios requiring strict consistency but slower due to synchronous operations.

2. **MissPolicyReturnThenAsyncWrite**:
   - Prioritizes low latency by generating and returning data immediately, updating Redis in the background.
   - Suitable for high-traffic systems where slight cache lag is acceptable.

3. **MissPolicyStaleWhileRevalidate**:
   - Returns stale data (if available) to maintain low latency while refreshing in the background. Falls back to synchronous generation if no stale data exists.
   - Useful for applications like content delivery where slightly outdated data is tolerable.

4. **MissPolicyFailFast**:
   - Fails immediately with an error on a miss, avoiding costly data generation.
   - Best for scenarios where missing data is a critical error or generation is too expensive.

5. **MissPolicyRefreshAhead**:
   - Proactively refreshes data when TTL is low (e.g., 20% remaining) to reduce cache misses. On miss, uses synchronous generation.
   - Ideal for frequently accessed data to ensure freshness with minimal misses.

6. **MissPolicyCooperativeRefresh**:
   - Handles concurrent misses by having one request generate data while others wait, ensuring efficiency under high concurrency.
   - Balances consistency and performance but may delay waiting requests.

7. **MissPolicyBestEffort**:
   - Attempts data generation but returns a zero value if it fails, prioritizing availability over correctness.
   - Useful for non-critical systems but requires callers to handle zero values.

8. **MissPolicyProbabilisticRefresh**:
   - Uses probabilistic early expiration to spread refresh load, reducing spikes at TTL boundaries. Generates synchronously on a miss.
   - Suited for large-scale systems but requires careful tuning to balance freshness and load.

## Summary
The cache miss policies provide a flexible set of strategies to handle cache misses, balancing latency, consistency, and availability. Choose `SyncWriteThenReturn` or `CooperativeRefresh` for high consistency, `ReturnThenAsyncWrite` or `StaleWhileRevalidate` for low latency, `RefreshAhead` or `ProbabilisticRefresh` for optimized performance, `BestEffort` for availability, and `FailFast` for strict error handling. Each policy is well-suited for specific scenarios, making the cache package versatile for various applications.
