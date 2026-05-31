# Redis Cache Wrapper

A high-performance, type-safe Redis caching library for Go with advanced features like background refresh, cache miss policies, and automatic data generation.

## � Table of Contents

- [Redis Cache Wrapper](#redis-cache-wrapper)
  - [📚 Table of Contents](#-table-of-contents)
  - [🚀 Features](#-features)
  - [🏗️ Architecture Overview](#️-architecture-overview)
    - [System Architecture Diagram](#system-architecture-diagram)
    - [Core Components](#core-components)
    - [Data Flow](#data-flow)
  - [🔄 Cache Miss Policies](#-cache-miss-policies)
    - [Miss Policy Decision Flow](#miss-policy-decision-flow)
    - [Policy Comparison](#policy-comparison)
  - [🔒 Concurrency & Locking](#-concurrency--locking)
    - [Keyed Mutex System](#keyed-mutex-system)
  - [⏱️ Background Operations](#️-background-operations)
    - [Background Refresh Flow](#background-refresh-flow)
    - [Refresh Cooldown Mechanism](#refresh-cooldown-mechanism)
  - [📦 Installation](#-installation)
  - [🛠 Prerequisites](#-prerequisites)
  - [🛠 Development](#-development)
  - [📖 Usage](#-usage)
  - [🧪 Testing](#-testing)
  - [📊 Performance Considerations](#-performance-considerations)
  - [🛣 Roadmap](#-roadmap)
  - [📄 License](#-license)
  - [🤝 Contributing](#-contributing)
  - [📞 Support](#-support)

## �🚀 Features

- **Type Safety**: Leverages Go 1.18+ generics for compile-time type safety
- **Three-Axis Cache Behaviour**: Independently configure miss-fill strategy (`MissFillPolicy`), hit-refresh strategy (`HitRefreshPolicy`), and error handling (`ErrorPolicy`)
- **Background Refresh**: Automatically refresh cached data in the background to reduce data staleness; mutex prevents cache stampede
- **Configurable TTL**: Set default and per-call TTL values
- **Thread Safety**: Built-in per-key locking prevents race conditions
- **JSON Serialization**: Automatic marshaling/unmarshaling of complex data types
- **Prefix Support**: Namespace your cache keys with configurable prefixes
- **Refresh Cooldown**: Prevent excessive background refreshes with configurable cooldowns

## 🏗️ Architecture Overview

### System Architecture Diagram

The cache system's behavior can be best understood through its interaction flows. The sequence diagram below illustrates three key scenarios:

1. **Cache Hit Flow**:
   - Initial key lookup in Redis
   - Check for staleness using lastRefreshByKey
   - Optional background refresh through Light Green section (governed by `HitRefreshPolicy`)
   - Immediate return of cached value

2. **Cache Miss Flow**:
   - Two example fill policies when data is not found:
     - `MissFillSync`: Block until data is generated and stored
     - `MissFillAsync`: Return generated data immediately, store asynchronously in Orchid section

3. **Background Operations**:
   - Light Green section: Background refresh for stale data
   - Orchid section: Async write workers for `MissFillAsync` policy
   - Lock management via KeyedMutex to prevent cache stampede
   - Cooldown checks to prevent excessive refreshes

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant H as Handler<T>
    participant KM as KeyedMutex
    participant R as Redis
    participant BG as Background<br/>Workers
    participant G as Generator<T>
    participant E as External<br/>Source

    rect rgba(100, 149, 237, 0.2)
        Note right of C: Cache Hit Flow
        C->>+H: GetOrRefresh(key, generator)
        H->>R: GET key
        R-->>H: Value exists
        H->>H: Check if stale<br/>(lastRefreshByKey)

        alt Needs Background Refresh
            rect rgba(144, 238, 144, 0.25)
                H->>BG: Spawn refresh worker
                Note over BG: Async with timeout
                BG->>KM: TryLock key
                BG->>G: Generate new value
                G->>E: Fetch data
                E-->>G: Return data
                G-->>BG: Return value
                BG->>R: SET key
                BG->>KM: Release lock
            end
        end
        H-->>-C: Return Result<T><br/>(fromCache: true)
    end

    rect rgba(255, 182, 193, 0.25)
        Note right of C: Cache Miss Flow
        C->>+H: GetOrRefresh(key, generator)
        H->>R: GET key
        R-->>H: Key not found

        alt SyncWriteThenReturn Policy (MissFillSync)
            H->>KM: TryLock key
            H->>G: Generate value
            G->>E: Fetch data
            E-->>G: Return data
            G-->>H: Return value
            H->>R: SET key
            H->>KM: Release lock
            H-->>-C: Return Result<T><br/>(fromCache: false)
        else ReturnThenAsyncWrite Policy (MissFillAsync)
            rect rgba(218, 112, 214, 0.25)
                H->>KM: TryLock key
                H->>G: Generate value
                G->>E: Fetch data
                E-->>G: Return data
                G-->>H: Return value
                H->>BG: Spawn write worker
                H-->>C: Return Result<T><br/>(fromCache: false)
                Note over BG: Async operation
                BG->>R: SET key
                BG->>KM: Release lock
            end
        end
    end
```

Key benefits of this architecture:
- **Type Safety**: Generic `Handler<T>` ensures compile-time type checking
- **Concurrency Control**: Per-key locking via `KeyedMutex` prevents cache stampede
- **Three Independent Axes**: Combine `MissFillPolicy`, `HitRefreshPolicy`, and `ErrorPolicy` freely
- **Background Refresh**: Keep cache fresh without blocking client requests
- **Cooldown Management**: Prevent excessive updates with configurable refresh intervals

### Core Components

The cache system consists of several key components working together:

#### 1. **Handler[T]** - Main Cache Interface

```mermaid
classDiagram
    class HandlerT["Handler[T]"] {
        +config handlerConfig
        +localLocks KeyedMutex
        +lastRefreshByKey map~string~time.Time
        +lastRefreshMu sync.Mutex
        +New(rdb, opts) Handler
        +Get(ctx, key) Result
        +Set(ctx, key, value, opts) error
        +GetOrRefresh(ctx, key, gen, opts) Result
    }
    class ResultT["Result[T]"] {
        +Value T
        +FromCache bool
        +CachedAt time.Time
    }
    class GeneratorT["Generator[T]"] {
        <<function>>
        +func(ctx) T, error
    }
    HandlerT --> ResultT : produces
    HandlerT --> GeneratorT : calls on miss
```

#### 2. **Configuration System**

```mermaid
flowchart LR
    A["Option functions\n(WithPrefix, WithDefaultTTL…)"] -->|applied at New| B["handlerConfig\n(handler defaults)"]
    C["CallOption functions\n(WithTTL, WithCallMissFillPolicy…)"] -->|applied per call| D["callOpts\n(per-call overrides)"]
    B --> E[GetOrRefresh]
    D -->|higher priority| E
    E --> F[Resolved behaviour]
```

### Data Flow

#### Cache Hit Scenario

```mermaid
sequenceDiagram
    participant C as Client
    participant H as Handler
    participant R as Redis
    participant BG as Background Goroutine
    participant G as Generator

    C->>H: GetOrRefresh(key, gen)
    H->>R: GET key
    R-->>H: value (cache hit)
    H-->>C: Result{fromCache: true}
    H->>BG: go spawnBackgroundRefresh (if policy triggers)
    BG->>H: TryLock(key)
    BG->>G: Generate(ctx)
    G-->>BG: new value
    BG->>R: SET key
    BG->>H: Unlock(key)
```

#### Cache Miss Scenario (MissFillSync)

```mermaid
sequenceDiagram
    participant C as Client
    participant H as Handler
    participant KM as KeyedMutex
    participant R as Redis
    participant G as Generator

    C->>H: GetOrRefresh(key, gen)
    H->>R: GET key
    R-->>H: redis.Nil (miss)
    H->>KM: Lock(key)
    H->>R: GET key (double-check)
    R-->>H: redis.Nil (still missing)
    H->>G: Generate(ctx)
    G-->>H: value
    H->>R: SET key
    H->>KM: Unlock(key)
    H-->>C: Result{fromCache: false}
```

## 🔄 Cache Behaviour Policies

Cache behaviour is controlled by **three independent axes** that can be combined freely:

| Axis | Type | Controls |
|------|------|----------|
| **Miss-fill** | `MissFillPolicy` | What happens when the key is not in the cache |
| **Hit-refresh** | `HitRefreshPolicy` | Whether/how to proactively refresh a key that was found |
| **Error handling** | `ErrorPolicy` | Whether generator errors surface to the caller |

### Miss-Fill Policy Decision Flow

```mermaid
flowchart TD
    A[GetOrRefresh called] --> B{Key in Redis?}
    B -->|Yes - Cache Hit| C{HitRefreshPolicy?}
    B -->|No - Cache Miss| D{MissFillPolicy?}

    C -->|DEFAULT| C1[Refresh if cooldown elapsed]
    C -->|AHEAD| C2[Refresh if remaining TTL pct below threshold]
    C -->|PROBABILISTIC| C3[XFetch: refresh with age-based probability]
    C -->|OLDER_THAN| C4[Refresh if entry age exceeds threshold]
    C -->|NONE| C5[No refresh]
    C1 & C2 & C3 & C4 --> C6[Spawn background refresh goroutine]
    C5 --> Z[Return cached value]
    C6 --> Z

    D -->|SYNC| D1[Lock → double-check → generate → write → return]
    D -->|ASYNC| D2[Generate → return → background write]
    D -->|STALE_OR_SYNC| D3{Stale data exists?}
    D -->|FAIL_FAST| D4[Return ErrCacheMiss immediately]
    D -->|COOPERATIVE| D5[Try-lock with timeout]
    D3 -->|Yes| D6[Return stale → background refresh]
    D3 -->|No| D1
    D5 -->|Lock acquired| D1
    D5 -->|Timeout| D7[Generate directly without caching]
```

### Miss-Fill Policy Comparison

| Policy | Response Time | Consistency | Stampede Protection | Best For |
|--------|---------------|-------------|---------------------|----------|
| `MissFillSync` *(default)* | Slower | Strong | Excellent (in-process lock) | Critical data consistency |
| `MissFillAsync` | Fastest | Eventual | Poor (no lock before gen) | High-performance APIs |
| `MissFillStaleOrSync` | Fast when stale exists | Eventual | Good | Content delivery, web apps |
| `MissFillFailFast` | Fastest | N/A | N/A | Circuit-breaker / explicit fallback |
| `MissFillCooperative` | Medium | Strong | Excellent (lock with timeout) | High concurrency, expensive generation |

### Hit-Refresh Policy Comparison

| Policy | Trigger | Best For |
|--------|---------|----------|
| `HitRefreshDefault` *(default)* | Every hit, gated by `refreshCooldown` | General use |
| `HitRefreshAhead` | Remaining TTL drops below threshold | Predictable workloads, avoid cold misses |
| `HitRefreshProbabilistic` | XFetch algorithm — probability rises with age | Distributed load distribution, large fleets |
| `HitRefreshOlderThan` | Entry age exceeds a configured threshold | Workloads with known staleness tolerance, time-sensitive data |
| `HitRefreshNone` | Never | Read-heavy, TTL expiry is acceptable |

### Error Policy

| Policy | Behaviour | Best For |
|--------|-----------|----------|
| `ErrorPolicySurface` *(default)* | Generator error returned to caller | Most cases |
| `ErrorPolicyZeroValue` | Error suppressed; caller receives zero value + nil error | Non-critical data, graceful degradation |

> `ErrCacheMiss` (returned by `MissFillFailFast`) is **never** suppressed by `ErrorPolicyZeroValue` — it is an intentional signal, not a generation failure.

```mermaid
flowchart LR
    subgraph SYNC["MissFillSync — strong consistency"]
        direction TB
        s1[Cache miss] --> s2[Acquire per-key lock]
        s2 --> s3[Double-check cache]
        s3 --> s4[Generate data]
        s4 --> s5[Write to Redis]
        s5 --> s6[Return data to caller]
    end
    subgraph ASYNC["MissFillAsync — lowest latency"]
        direction TB
        a1[Cache miss] --> a2[Generate data]
        a2 --> a3[Return data to caller immediately]
        a3 --> a4[Background: try-lock]
        a4 --> a5[Background: double-check and write]
    end
```

> **Stampede note (MissFillAsync)**: The background write is protected by a try-lock, but every concurrent caller in the *first miss wave* still invokes the generator. Use `WithMissDeduplicationWindow` to suppress duplicate generation after the first write.

## 🔒 Concurrency & Locking

### Keyed Mutex System

The cache uses a per-key locking mechanism to prevent race conditions and cache stampede. Different keys never block each other.

```mermaid
flowchart TD
    A[Request for Key X] --> B[keyedMutex.Lock X]
    B --> C{Channel exists\nfor Key X?}
    C -->|Yes| D[Use existing channel]
    C -->|No| E[Create new buffered channel]
    D & E --> F[Send to channel]
    F --> G{Channel slot\navailable?}
    G -->|Yes| H[Lock acquired immediately]
    G -->|No| I[Block until prior holder reads]
    H & I --> J[Perform cache operation]
    J --> K[Read from channel — release lock]
    K --> L[Return unlock function to caller]
```

**Key benefits:**
- **Prevents cache stampede**: only one goroutine per key can generate data at a time
- **Fine-grained**: different keys never block each other
- **Memory efficient**: channels created on demand, cleaned up on unlock
- **Deadlock safe**: simple channel-based implementation with no nested locks

## ⏱️ Background Operations

### Background Refresh Flow

Background refresh keeps cached data fresh without blocking client requests:

```mermaid
sequenceDiagram
    participant C as Client
    participant H as Handler
    participant R as Redis
    participant BG as Background Goroutine
    participant G as Generator

    C->>H: GetOrRefresh(key, gen)
    H->>R: GET key
    R-->>H: value (cache hit)
    H-->>C: Result{fromCache: true}
    H->>BG: go spawnBackgroundRefresh(key)
    BG->>H: TryLock(key)
    alt Cooldown has elapsed
        BG->>G: Generate(ctx)
        G-->>BG: new value
        BG->>R: SET key
        BG->>H: setLastRefreshNow(key)
    end
    BG->>H: Unlock(key)
```

### Refresh Cooldown Mechanism

The cooldown mechanism prevents excessive background refreshes:

```mermaid
flowchart TD
    A[Background refresh triggered] --> B{refreshCooldown > 0?}
    B -->|No| E[Allow refresh]
    B -->|Yes| C{Key in lastRefreshByKey?}
    C -->|No| E
    C -->|Yes| D{time.Since last >= cooldown?}
    D -->|Yes| E
    D -->|No| F[Skip refresh — cooldown not yet elapsed]
    E --> G[Perform refresh]
    G --> H[Update lastRefreshByKey timestamp]
```

## 📦 Installation

```bash
go get github.com/Hossein-Roshandel/cashcov
```

## 🛠 Prerequisites

- Go 1.21 or later (for generics support)
- Redis server
- `github.com/redis/go-redis/v9` client

## 🛠 Development

### Docker Development Environment

The project includes a complete Docker-based development environment for consistent development across different machines.

#### Quick Start with Docker

1. **Start the development environment:**
   ```bash
   ./docker-dev.sh up
   ```

2. **Run tests in the container:**
   ```bash
   ./docker-dev.sh test
   ```

3. **Run linting:**
   ```bash
   ./docker-dev.sh lint
   ```

4. **Access the container shell:**
   ```bash
   ./docker-dev.sh shell
   ```

#### Available Docker Commands

The `docker-dev.sh` script provides convenient commands:

```bash
./docker-dev.sh up          # Start development environment
./docker-dev.sh down        # Stop development environment
./docker-dev.sh build       # Build Docker images
./docker-dev.sh rebuild     # Rebuild from scratch
./docker-dev.sh shell       # Open container shell
./docker-dev.sh test        # Run tests
./docker-dev.sh lint        # Run linting
./docker-dev.sh logs        # Show logs
./docker-dev.sh clean       # Clean up containers
```

#### VS Code Integration

The development environment is fully integrated with VS Code:

1. **Dev Containers**: Use "Dev Containers: Reopen in Container" to develop inside Docker
2. **Debugging**: Launch configurations for local and remote debugging
3. **Tasks**: Pre-configured tasks for testing, linting, and formatting

#### What's Included

- **Go 1.25.1**: Latest Go version with full toolchain
- **Development Tools**: golangci-lint, staticcheck, goimports, pre-commit
- **Redis Server**: Local Redis instance for testing
- **Hot Reload**: Volume mounting for instant code changes
- **Debug Support**: Delve debugger configured for remote debugging

### Local Development Setup

If you prefer local development:

```bash
./setup-dev.sh
```

This installs all necessary tools and sets up pre-commit hooks.

### Quality Checks

Use the Makefile for common development tasks:

```bash
make help        # Show all available commands
make test        # Run tests
make lint        # Run linter
make fmt         # Format code
make ci          # Run all CI checks locally
```

### Code Quality Tools

- **golangci-lint**: Comprehensive linting with 30+ linters
- **gosec**: Security vulnerability scanner
- **pre-commit**: Git hooks for automatic quality checks
- **GitHub Actions**: CI/CD pipeline with testing, linting, and security scanning

## 📖 Usage

### Basic Setup

```go
package main

import (
    "context"
    "fmt"
    "time"

    "github.com/redis/go-redis/v9"
    "your-module/cache"
)

func main() {
    // Initialize Redis client
    rdb := redis.NewClient(&redis.Options{
        Addr: "localhost:6379",
    })

    // Create a type-safe cache handler for strings
    handler := cache.New[string](rdb,
        cache.WithPrefix("myapp"),
        cache.WithDefaultTTL(5*time.Minute),
        cache.WithRefreshCooldown(30*time.Second),
    )

    ctx := context.Background()

    // Basic Set/Get operations
    err := handler.Set(ctx, "user:123", "john_doe")
    if err != nil {
        panic(err)
    }

    result, err := handler.Get(ctx, "user:123")
    if err != nil {
        panic(err)
    }

    fmt.Printf("Value: %s, FromCache: %t\n", result.Value, result.FromCache)
}
```

### Advanced Usage with Data Generation

```go
type User struct {
    ID       int    `json:"id"`
    Username string `json:"username"`
    Email    string `json:"email"`
}

func main() {
    rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})

    // Create a cache handler for User structs
    userCache := cache.New[User](rdb,
        cache.WithPrefix("users"),
        cache.WithDefaultTTL(10*time.Minute),
        cache.WithBackgroundRefreshTimeout(5*time.Second),
        cache.WithMissFillPolicy(cache.MissFillSync),
    )

    ctx := context.Background()

    // Generator function that fetches user data
    userGenerator := func(ctx context.Context) (User, error) {
        // Simulate database lookup or API call
        return User{
            ID:       123,
            Username: "john_doe",
            Email:    "john@example.com",
        }, nil
    }

    // GetOrRefresh will use cached data if available,
    // or generate and cache new data if missing
    result, err := userCache.GetOrRefresh(ctx, "123", userGenerator)
    if err != nil {
        panic(err)
    }

    fmt.Printf("User: %+v, FromCache: %t\n", result.Value, result.FromCache)
}
```

### Cache Miss-Fill Policies

#### Sync Fill (Default)
```go
// On cache miss: acquire lock, double-check, generate, write, return
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissFillPolicy(cache.MissFillSync),
)
```

#### Async Fill
```go
// On cache miss: generate and return immediately, write to cache in background
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissFillPolicy(cache.MissFillAsync),
)
```

#### Stale-While-Revalidate Fill
```go
// On cache miss: return stale data if available, refresh in background;
// requires WithStaleDataTTL on the handler
handler := cache.New[string](rdb,
    cache.WithStaleDataTTL(24*time.Hour),
)
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissFillPolicy(cache.MissFillStaleOrSync),
)
```

#### Fail-Fast
```go
// On cache miss: return ErrCacheMiss immediately, no generation
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallMissFillPolicy(cache.MissFillFailFast),
)
if errors.Is(err, cache.ErrCacheMiss) {
    // handle explicitly
}
```

### Hit-Refresh Policies

```go
// Refresh-ahead: trigger background refresh when TTL drops below threshold
handler := cache.New[string](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshAhead),
    cache.WithRefreshAheadThreshold(0.2), // refresh when 20% TTL remains
)

// Probabilistic: XFetch — probability of refresh grows as entry ages
handler := cache.New[string](rdb,
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshProbabilistic),
    cache.WithProbabilisticBeta(1.0),
)
```

### Error Policy

```go
// Suppress generator errors — return zero value + nil error instead
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithCallErrorPolicy(cache.ErrorPolicyZeroValue),
)
// err is nil even if generator failed; result.Value is the zero value
```

### Configuration Options

#### Handler-Level Options
```go
handler := cache.New[string](rdb,
    cache.WithPrefix("myapp"),                             // Key prefix
    cache.WithDefaultTTL(5*time.Minute),                  // Default expiration
    cache.WithBackgroundRefreshTimeout(3*time.Second),    // Background refresh timeout
    cache.WithRefreshCooldown(1*time.Minute),             // Min time between refreshes
    cache.WithMissFillPolicy(cache.MissFillSync),         // Default miss-fill policy
    cache.WithDefaultHitRefreshPolicy(cache.HitRefreshDefault), // Default hit-refresh policy
    cache.WithDefaultErrorPolicy(cache.ErrorPolicySurface),     // Default error policy
)
```

#### Call-Level Options
```go
result, err := handler.GetOrRefresh(ctx, "key", generator,
    cache.WithTTL(30*time.Minute),                        // Override TTL for this call
    cache.WithoutBackgroundRefresh(),                     // Disable background refresh
    cache.WithCallMissFillPolicy(cache.MissFillAsync),    // Override miss-fill policy
    cache.WithCallHitRefreshPolicy(cache.HitRefreshAhead),// Override hit-refresh policy
    cache.WithCallErrorPolicy(cache.ErrorPolicyZeroValue),// Override error policy
)
```

## � API Reference

### Key Methods

| Component | Methods/Functions |
|-----------|------------------|
| **Handler<T>** | `New(rdb *redis.Client, opts ...Option) Handler<T>` |
|               | `Get(ctx context.Context, key string) Result<T>` |
|               | `Set(ctx context.Context, key string, value T, opts ...CallOption) error` |
|               | `GetOrRefresh(ctx context.Context, key string, gen Generator<T>, opts ...CallOption) Result<T>` |
| **Handler Options** | `WithPrefix(prefix string) Option` |
|                    | `WithDefaultTTL(ttl time.Duration) Option` |
|                    | `WithBackgroundRefreshTimeout(d time.Duration) Option` |
|                    | `WithRefreshCooldown(d time.Duration) Option` |
|                    | `WithMissFillPolicy(p MissFillPolicy) Option` |
|                    | `WithDefaultHitRefreshPolicy(p HitRefreshPolicy) Option` |
|                    | `WithDefaultErrorPolicy(p ErrorPolicy) Option` |
|                    | `WithStaleDataTTL(ttl time.Duration) Option` |
|                    | `WithRefreshAheadThreshold(threshold float64) Option` |
|                    | `WithProbabilisticBeta(beta float64) Option` |
|                    | `WithCooperativeTimeout(timeout time.Duration) Option` |
| **Call Options** | `WithTTL(ttl time.Duration) CallOption` |
|                 | `WithoutBackgroundRefresh() CallOption` |
|                 | `WithCallMissFillPolicy(p MissFillPolicy) CallOption` |
|                 | `WithCallHitRefreshPolicy(p HitRefreshPolicy) CallOption` |
|                 | `WithCallErrorPolicy(p ErrorPolicy) CallOption` |
|                 | `WithStaleCheckTimeout(timeout time.Duration) CallOption` |

### Method Flow Diagrams

#### GetOrRefresh Method Flow

```mermaid
flowchart TD
    A[GetOrRefresh called] --> B[Resolve TTL and three policy axes]
    B --> C[Redis GET key]
    C --> D{Cache hit?}
    D -->|Yes| E[handleHitRefresh async]
    E --> F[Return Result from cache]
    D -->|"No — redis.Nil"| G{missDeduplicationWindow > 0?}
    G -->|Yes| H{Written within window by this process?}
    H -->|Yes| I[Retry Redis GET]
    I --> J{Found?}
    J -->|Yes| F
    J -->|No| K[Dispatch fill policy]
    H -->|No| K
    G -->|No| K
    K --> L{MissFillPolicy?}
    L -->|SYNC| M[missSyncWriteThenReturn]
    L -->|ASYNC| N[missReturnThenAsyncWrite]
    L -->|STALE_OR_SYNC| O[missStaleWhileRevalidate]
    L -->|FAIL_FAST| P["missFailFast → ErrCacheMiss"]
    L -->|COOPERATIVE| Q[missCooperativeRefresh]
    M & N & O & Q --> R{ErrorPolicy?}
    P --> S[Return ErrCacheMiss]
    R -->|SURFACE| T[Return result or wrapped error]
    R -->|ZERO_VALUE| U{Is error ErrCacheMiss?}
    U -->|Yes| S
    U -->|No| V[Suppress error, return zero-value Result]
```

## 🔧 Advanced Configuration

### Configuration Hierarchy

The cache system uses a two-level configuration approach:

```mermaid
flowchart TD
    A["Option functions\n(WithPrefix, WithDefaultTTL…)"] -->|applied at New| B["handlerConfig\n• prefix\n• defaultTTL\n• bgRefreshTimeout\n• refreshCooldown\n• defaultMissFillPolicy\n• defaultHitRefreshPolicy\n• defaultErrorPolicy\n• missDeduplicationWindow"]
    C["CallOption functions\n(WithTTL, WithCallMissFillPolicy…)"] -->|applied per-call| D["callOpts\n• ttl override\n• overrideMissFillPolicy\n• overrideHitRefreshPolicy\n• overrideErrorPolicy\n• refreshOlderThanAge"]
    B --> E[GetOrRefresh]
    D -->|overrides handler defaults| E
    E --> F[Resolved behaviour for this call]
```

### Performance Tuning Guide

| Setting | Low Latency | High Throughput | Memory Efficient |
|---------|-------------|-----------------|------------------|
| **defaultTTL** | 1-5 minutes | 10-30 minutes | 1-2 minutes |
| **bgRefreshTimeout** | 1-2 seconds | 5-10 seconds | 3-5 seconds |
| **refreshCooldown** | 10-30 seconds | 1-5 minutes | 30-60 seconds |
| **MissFillPolicy** | `MissFillAsync` | `MissFillSync` | `MissFillSync` |
| **HitRefreshPolicy** | `HitRefreshDefault` | `HitRefreshAhead` | `HitRefreshNone` |

```mermaid
flowchart TD
    A[Primary concern?] --> B[Low latency]
    A --> C[High throughput]
    A --> D[Memory efficient]
    B --> B1["MissFillAsync\nShort TTL\nQuick timeouts\nHitRefreshDefault"]
    C --> C1["MissFillSync\nLonger TTL\nLonger timeouts\nHitRefreshAhead"]
    D --> D1["MissFillSync\nShort TTL\nLong cooldowns\nHitRefreshNone"]
```

## 🎯 Use Cases

### Web Applications
```go
// Cache expensive database queries
func GetUserProfile(userID string) (User, error) {
    return userCache.GetOrRefresh(ctx, userID, func(ctx context.Context) (User, error) {
        return fetchUserFromDatabase(userID)
    })
}
```

### API Response Caching
```go
// Cache external API responses
func GetWeatherData(city string) (Weather, error) {
    return weatherCache.GetOrRefresh(ctx, city, func(ctx context.Context) (Weather, error) {
        return callWeatherAPI(city)
    }, cache.WithTTL(30*time.Minute))
}
```

### Computational Results
```go
// Cache expensive calculations
func GetAnalyticsReport(params AnalyticsParams) (Report, error) {
    key := fmt.Sprintf("analytics:%s", params.Hash())
    return reportCache.GetOrRefresh(ctx, key, func(ctx context.Context) (Report, error) {
        return generateAnalyticsReport(params)
    })
}
```

## 🧪 Testing

The package includes comprehensive tests with Redis mocking:

```bash
go test -v
```

Key test scenarios:
- Basic Set/Get operations
- Cache hits and misses
- Background refresh functionality
- Concurrent access safety
- JSON marshaling/unmarshaling
- Error handling
- Refresh cooldown behavior

## 📊 Performance Considerations

### Memory Usage
- Keyed mutexes are kept in memory for active keys
- Background goroutines are spawned for async operations
- Refresh timestamps are tracked for cooldown functionality

### Concurrency
- Per-key locking prevents cache stampede
- Background operations don't block main execution
- Thread-safe operations across all methods

### Redis Operations
- Efficient use of Redis commands (GET, SET, EXISTS)
- JSON marshaling overhead for complex types
- Configurable timeouts for all operations

### Multi-Language Bindings

The library ships a Python package (`python/`) built on top of a CGo shared
library (`cshim/`). All three policy axes are fully exposed to Python via
`IntEnum` constants that mirror the Go iota values exactly.

```python
import json
from cashcov import CacheHandler
from cashcov.policies import MissFillPolicy, HitRefreshPolicy, ErrorPolicy

# Handler-level policy defaults
with CacheHandler(
    redis_addr="localhost:6379",
    prefix="myapp",
    ttl=300,
    miss_fill_policy=MissFillPolicy.ASYNC,
    hit_refresh_policy=HitRefreshPolicy.AHEAD,
    refresh_ahead_threshold=0.2,
    error_policy=ErrorPolicy.ZERO_VALUE,
) as cache:
    def generate(key: str) -> str:
        return json.dumps({"result": f"computed for {key}"})

    # Per-call override — bypass dedup and fail fast if no cached value
    raw = cache.get_or_refresh(
        "my-key",
        generate,
        miss_fill_policy=MissFillPolicy.FAIL_FAST,
    )
    if raw:
        data = json.loads(raw)
```

Build and install:

```bash
# Directly from GitHub (no clone needed):
pip install git+https://github.com/Hossein-Roshandel/cashcov.git#subdirectory=python

# From a local clone:
make build-shim          # compiles libcashcov.so → python/cashcov/
pip install -e python/   # editable install (also auto-compiles the shim)
```

See `python/README.md` for the full API reference and all five example scripts.

#### Future: gRPC service wrapper

The CGo binding requires the shared library to be compiled for each target
platform and architecture.  A planned alternative is a standalone **gRPC
service** that wraps this library:

```
cmd/cashcov-server/   ← Go binary (thin gRPC server over the library)
proto/cashcov/v1/     ← Protobuf service definition (language-neutral)
python/cashcov/       ← Python gRPC client (generated stubs + high-level API)
```

Benefits over the CGo approach:

| | CGo shared library | gRPC service |
|---|---|---|
| No compilation per platform | ✗ | ✓ |
| In-process, zero network overhead | ✓ | ✗ (loopback) |
| Works from any language without recompile | ✗ | ✓ |
| Stampede protection crosses processes | ✗ | ✓ |
| Easy horizontal scaling | ✗ | ✓ (sidecar) |

The gRPC approach is the recommended path for production multi-language
deployments and is tracked in the roadmap below.

## 🛣 Roadmap

### Planned Features
- [ ] **Metrics & Observability**: Built-in metrics for hit rates, generation times, and error rates
- [ ] **Circuit Breaker**: Automatic fallback when cache or generators fail repeatedly
- [ ] **Cache Warming**: Pre-populate cache with commonly accessed data
- [ ] **Batch Operations**: Support for getting/setting multiple keys efficiently
- [ ] **Custom Serializers**: Support for non-JSON serialization (protobuf, msgpack, etc.)
- [ ] **Cache Tagging**: Group related cache entries for bulk invalidation
- [ ] **LRU Eviction**: Local in-memory LRU cache layer for ultra-fast access
- [ ] **Distributed Locking**: Replace local locks with Redis-based distributed locks
- [ ] **Configuration Validation**: Compile-time and runtime configuration validation
- [ ] **Cache Compression**: Optional compression for large cached values

### Performance Improvements
- [ ] **Connection Pooling**: Optimize Redis connection usage
- [ ] **Pipelining**: Batch Redis operations for better throughput
- [ ] **Memory Optimization**: Reduce memory footprint of internal structures
- [ ] **Hot Key Detection**: Identify and optimize frequently accessed keys

### Developer Experience
- [ ] **Middleware Integration**: Built-in middleware for popular Go frameworks
- [ ] **CLI Tools**: Command-line utilities for cache management and debugging
- [ ] **Monitoring Dashboard**: Web UI for cache statistics and management
- [ ] **Documentation**: Interactive examples and best practices guide
- [ ] **Benchmarking Suite**: Performance testing and comparison tools

### Enterprise Features
- [ ] **Multi-Region Support**: Cross-region cache synchronization
- [ ] **Access Control**: Key-level permissions and authentication
- [ ] **Audit Logging**: Detailed logging of cache operations
- [ ] **Backup/Restore**: Tools for cache data backup and recovery
- [ ] **Rate Limiting**: Built-in rate limiting for cache operations

## 📄 License

MIT License ?

## 🤝 Contributing

Contributions are welcome!

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📞 Support

- Create an issue for bug reports or feature requests
- Check existing issues before creating new ones
- Provide minimal reproduction cases for bugs

## 🔗 Related Open Source Packages

This section lists other open source caching libraries and their approach to cache miss policies for reference and comparison.

### Go Caching Libraries

#### [patrickmn/go-cache](https://github.com/patrickmn/go-cache)
- **Focus**: In-memory caching with expiration
- **Miss Policies**: Basic expiration-based eviction
- **Notable Features**: Thread-safe, cleanup intervals
- **Use Case**: Simple in-memory caching without Redis

#### [allegro/bigcache](https://github.com/allegro/bigcache)
- **Focus**: High-performance in-memory cache
- **Miss Policies**: LRU-based eviction, no custom miss handling
- **Notable Features**: Zero GC overhead, fast concurrent access
- **Use Case**: High-throughput in-memory caching

#### [dgraph-io/ristretto](https://github.com/dgraph-io/ristretto)
- **Focus**: High-performance cache with admission control
- **Miss Policies**: TinyLFU admission policy, cost-based eviction
- **Notable Features**: Probabilistic admission control, metrics
- **Use Case**: Smart caching with admission control

#### [coocood/freecache](https://github.com/coocood/freecache)
- **Focus**: Zero GC overhead cache
- **Miss Policies**: LRU eviction, basic expiration
- **Notable Features**: Off-heap storage, no GC pressure
- **Use Case**: Large-scale in-memory caching

### Multi-Language Caching Solutions

#### [ben-manes/caffeine](https://github.com/ben-manes/caffeine) (Java)
- **Focus**: High-performance Java caching library
- **Miss Policies**:
  - Refresh-ahead (similar to our `MissPolicyRefreshAhead`)
  - Async refresh (similar to our `MissPolicyReturnThenAsyncWrite`)
  - Custom loading strategies
- **Notable Features**: W-TinyLFU admission, async loading
- **Comparison**: Most similar to our approach with multiple miss policies

#### [Netflix/EVCache](https://github.com/Netflix/EVCache) (Java)
- **Focus**: Distributed caching for AWS
- **Miss Policies**:
  - Fail-fast patterns (similar to our `MissPolicyFailFast`)
  - Cross-region replication
  - Bulk loading strategies
- **Notable Features**: Multi-zone replication, monitoring
- **Use Case**: Large-scale distributed caching

#### [Twitter/twemcache](https://github.com/twitter/twemcache) (C)
- **Focus**: Memcached fork with additional features
- **Miss Policies**: Custom eviction policies, slab allocation
- **Notable Features**: Better memory management than memcached
- **Use Case**: High-performance memcached replacement

### Redis-Specific Libraries

#### [go-redis/cache](https://github.com/go-redis/cache) (Go)
- **Focus**: Simple Redis caching with marshaling
- **Miss Policies**: Basic cache-aside pattern only
- **Notable Features**: Multiple serialization formats
- **Comparison**: Simpler approach, no advanced miss policies

#### [vmihailenco/msgpack](https://github.com/vmihailenco/msgpack) + Redis (Go)
- **Focus**: Efficient serialization for Redis
- **Miss Policies**: Manual implementation required
- **Notable Features**: Fast binary serialization
- **Use Case**: When serialization performance is critical

#### [muesli/cache2go](https://github.com/muesli/cache2go) (Go)
- **Focus**: Concurrency-safe in-memory caching
- **Miss Policies**: Callback-based loading, expiration
- **Notable Features**: Data loading callbacks, access counting
- **Comparison**: Similar callback approach but in-memory only

### Web Framework Cache Middleware

#### [gin-contrib/cache](https://github.com/gin-contrib/cache) (Go/Gin)
- **Focus**: HTTP response caching middleware
- **Miss Policies**: Simple cache-aside with TTL
- **Notable Features**: Redis and in-memory backends
- **Use Case**: HTTP response caching

#### [labstack/echo-contrib](https://github.com/labstack/echo-contrib) (Go/Echo)
- **Focus**: Echo framework middleware collection
- **Miss Policies**: Basic caching middleware
- **Notable Features**: Multiple storage backends
- **Use Case**: Web framework integration

### Academic and Research Projects

#### [Guava Cache](https://github.com/google/guava) (Java)
- **Focus**: Google's core Java libraries
- **Miss Policies**:
  - LoadingCache with refresh-ahead
  - Probabilistic eviction
  - Size and time-based eviction
- **Notable Features**: Statistical tracking, concurrent loading
- **Comparison**: Similar refresh strategies to our implementation

#### [Varnish Cache](https://github.com/varnishcache/varnish-cache) (C)
- **Focus**: HTTP accelerator and reverse proxy
- **Miss Policies**:
  - Stale-while-revalidate (similar to our `MissPolicyStaleWhileRevalidate`)
  - Grace mode (serving stale content)
  - Custom VCL policies
- **Notable Features**: VCL scripting language, HTTP-specific optimizations
- **Comparison**: Web-focused but similar stale-while-revalidate concept

### Industry-Specific Solutions

#### [Shopify/go-lua](https://github.com/Shopify/go-lua) + Redis Lua Scripts
- **Focus**: Custom Redis scripting for complex cache logic
- **Miss Policies**: Custom Lua-based policies
- **Notable Features**: Server-side logic execution
- **Use Case**: Complex cache policies requiring server-side logic

#### [bradfitz/gomemcache](https://github.com/bradfitz/gomemcache) (Go)
- **Focus**: Pure Go memcached client
- **Miss Policies**: Basic memcached operations only
- **Notable Features**: Simple, reliable memcached access
- **Use Case**: When memcached is preferred over Redis

## Key Differentiators of This Package

| Feature | This Package | Other Go Libraries | Notes |
|---------|-------------|-------------------|-------|
| **Generic Type Safety** | ✅ Full generics support | ❌ Most use interface{} | Compile-time type safety |
| **Three-Axis Policy Design** | ✅ Miss-fill, hit-refresh, error handling are independent | ❌ Usually a single flat policy enum | Compose behaviours freely |
| **Stale-While-Revalidate** | ✅ `MissFillStaleOrSync` | ❌ Rare in Go libraries | Common in web caching |
| **Probabilistic Refresh** | ✅ `HitRefreshProbabilistic` (XFetch) | ❌ Not commonly implemented | Load distribution |
| **Refresh-Ahead** | ✅ `HitRefreshAhead`, TTL-threshold based | ❌ Limited implementations | Prevents cold misses |
| **Cooperative Refresh** | ✅ `MissFillCooperative` (lock + timeout fallback) | ⚠️ Basic locking only | Advanced concurrency control |
| **Per-Key Locking** | ✅ Fine-grained mutex system | ⚠️ Often global locks | Reduces contention |
| **Graceful Degradation** | ✅ `ErrorPolicyZeroValue` call option | ❌ Usually hard-coded | Composable, not pre-baked |

### Inspiration and Research Sources

This implementation draws inspiration from:
- **Caffeine** (Java) - Multi-policy approach and refresh-ahead
- **Varnish** - Stale-while-revalidate concepts
- **Netflix EVCache** - Fail-fast and distributed patterns
- **Academic papers** on probabilistic caching and cache stampede prevention
- **CDN technologies** like Cloudflare and Fastly for SWR patterns
- **Database caching patterns** from high-scale web applications

### Performance Comparisons

While we haven't conducted formal benchmarks against all libraries, our design focuses on:
- **Lower allocation overhead** through generics (vs interface{} casting)
- **Reduced lock contention** via per-key locking (vs global locks)
- **Better miss handling** through diverse policy options
- **Improved cache hit rates** via proactive refresh strategies

---

Built with ❤️ for the Go community
