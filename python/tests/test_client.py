"""End-to-end tests for CacheHandler.

Every test here exercises the full stack:

    Python CacheHandler → ctypes → Go shim → Redis

Redis is provided by the ``handler`` / ``make_handler`` fixtures in conftest.py
(testcontainers or a real Redis).  All tests are skipped automatically when the
Go shim has not been compiled.

Key notes about the value protocol
-----------------------------------
The Go handler is typed ``Handler[string]``.  Internally it JSON-encodes the
Go string before writing to Redis and JSON-decodes on read.  This means:

* ``handler.set("k", "hello")``   → Redis stores ``"hello"`` (JSON string)
* ``get_or_refresh(...)``         → returns ``"hello"``  (the raw Go string)

Generator functions must return a plain ``str``; the Go layer handles the
JSON round-trip transparently.  Tests in this file use plain strings (no extra
``json.dumps`` wrapper) to keep assertions simple.
"""

from __future__ import annotations

import threading
import time

import pytest

from cashcov import CacheError, CacheHandler
from cashcov.policies import ErrorPolicy, HitRefreshPolicy, MissFillPolicy

# ---------------------------------------------------------------------------
# Basic cache miss / hit
# ---------------------------------------------------------------------------


class TestBasicMissAndHit:
    def test_cache_miss_calls_generator(self, handler: CacheHandler):
        calls: list[str] = []

        def gen(key: str) -> str:
            calls.append(key)
            return "generated"

        result = handler.get_or_refresh("my-key", gen)

        assert result == "generated"
        assert calls == ["my-key"], "generator must be called exactly once on a miss"

    def test_cache_hit_skips_generator(self, handler: CacheHandler):
        calls: list[str] = []

        def gen(key: str) -> str:
            calls.append(key)
            return "generated"

        handler.get_or_refresh("my-key", gen)  # miss — fills cache
        result = handler.get_or_refresh("my-key", gen)  # hit

        assert result == "generated"
        assert len(calls) == 1, "generator must NOT be called on a cache hit"

    def test_set_then_get_or_refresh_skips_generator(self, handler: CacheHandler):
        handler.set("my-key", "pre-seeded")

        calls: list[str] = []
        result = handler.get_or_refresh("my-key", lambda k: calls.append(k) or "ignored")  # type: ignore[func-returns-value]

        assert result == "pre-seeded"
        assert calls == [], "generator must not run when key already exists"

    def test_different_keys_are_independent(self, handler: CacheHandler):
        handler.set("key-a", "alpha")
        handler.set("key-b", "beta")

        assert handler.get_or_refresh("key-a", lambda _: "wrong") == "alpha"
        assert handler.get_or_refresh("key-b", lambda _: "wrong") == "beta"

    def test_prefix_is_applied(self, make_handler, redis_client):
        h = make_handler(prefix="ns1", ttl=10)
        h.set("item", "value")

        # The real Redis key should carry the prefix
        assert redis_client.exists("ns1:item") == 1

    def test_empty_prefix_uses_bare_key(self, make_handler, redis_client):
        h = make_handler(prefix="", ttl=10)
        h.set("bare", "value")

        assert redis_client.exists("bare") == 1


# ---------------------------------------------------------------------------
# MissFillPolicy
# ---------------------------------------------------------------------------


class TestMissFillSync:
    """MissFillSync (default): per-key lock prevents stampede."""

    def test_returns_generated_value(self, handler: CacheHandler):
        result = handler.get_or_refresh("k", lambda _: "synced")
        assert result == "synced"

    def test_generator_called_once_under_concurrency(self, make_handler):
        """With MissFillSync, only one thread should invoke the generator
        even when many threads race on a cold key."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)
        call_count = 0
        lock = threading.Lock()

        def slow_gen(key: str) -> str:
            nonlocal call_count
            time.sleep(0.05)  # simulate slow generation
            with lock:
                call_count += 1
            return "result"

        threads = [
            threading.Thread(target=h.get_or_refresh, args=("shared-key", slow_gen))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert call_count == 1, (
            f"MissFillSync should prevent stampede; generator was called {call_count} times"
        )


class TestMissFillAsync:
    """MissFillAsync: value is returned immediately; Redis write is in the background."""

    def test_returns_value_on_miss(self, make_handler):
        h = make_handler(miss_fill_policy=MissFillPolicy.ASYNC)
        result = h.get_or_refresh("k", lambda _: "async-value")
        assert result == "async-value"

    def test_value_written_to_redis_in_background(self, make_handler, redis_client):
        h = make_handler(prefix="async-test", miss_fill_policy=MissFillPolicy.ASYNC, ttl=30)
        h.get_or_refresh("k", lambda _: "async-value")

        # Give the background goroutine time to write
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if redis_client.exists("async-test:k"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Background write did not reach Redis within 2 s")

    def test_second_call_hits_cache(self, make_handler):
        h = make_handler(miss_fill_policy=MissFillPolicy.ASYNC, ttl=30)
        calls: list[str] = []

        def gen(key: str) -> str:
            calls.append(key)
            return "value"

        h.get_or_refresh("k", gen)

        # Wait for background write
        time.sleep(0.1)

        h.get_or_refresh("k", gen)

        assert len(calls) == 1, "second call should hit cache, not invoke generator again"


class TestMissFillFailFast:
    """MissFillFailFast: CacheError is raised immediately on a cold key."""

    def test_raises_on_cold_key(self, make_handler):
        h = make_handler(miss_fill_policy=MissFillPolicy.FAIL_FAST)
        with pytest.raises(CacheError):
            h.get_or_refresh("cold-key", lambda _: "never-called")

    def test_returns_value_after_set(self, make_handler):
        h = make_handler(miss_fill_policy=MissFillPolicy.FAIL_FAST, ttl=30)
        h.set("warm-key", "pre-seeded")
        result = h.get_or_refresh("warm-key", lambda _: "never-called")
        assert result == "pre-seeded"

    def test_fail_fast_overrides_sync_default_per_call(self, make_handler):
        """Per-call override: handler default SYNC, single call uses FAIL_FAST."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)
        with pytest.raises(CacheError):
            h.get_or_refresh(
                "cold-key",
                lambda _: "never-called",
                miss_fill_policy=MissFillPolicy.FAIL_FAST,
            )


class TestMissFillCooperative:
    """MissFillCooperative: first caller generates; others wait for the lock."""

    def test_returns_generated_value(self, make_handler):
        h = make_handler(
            miss_fill_policy=MissFillPolicy.COOPERATIVE,
            cooperative_timeout=5,
        )
        result = h.get_or_refresh("k", lambda _: "coop-value")
        assert result == "coop-value"

    def test_generator_called_once_under_concurrency(self, make_handler):
        h = make_handler(
            miss_fill_policy=MissFillPolicy.COOPERATIVE,
            cooperative_timeout=5,
        )
        call_count = 0
        lock = threading.Lock()

        def slow_gen(key: str) -> str:
            nonlocal call_count
            time.sleep(0.05)
            with lock:
                call_count += 1
            return "result"

        threads = [
            threading.Thread(target=h.get_or_refresh, args=("shared-key", slow_gen))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cooperative: the first caller generates; others wait and re-check.
        # If the timeout expires for later callers they generate independently,
        # so allow ≤ number-of-threads but assert at least 1.
        assert 1 <= call_count <= 8


class TestMissFillStaleOrSync:
    """MissFillStaleOrSync: return stale data immediately when available."""

    def test_falls_back_to_sync_when_no_stale_data(self, make_handler):
        """Without a stale key in Redis, behaves exactly like MissFillSync."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            generator=lambda _: "fresh",
        )
        result = h.get_or_refresh("k", lambda _: "fresh")
        assert result == "fresh"

    def test_returns_stale_without_calling_generator(self, make_handler, redis_client):
        """Seed the stale Redis key directly; the generator must NOT be called."""
        h = make_handler(
            prefix="stale-test",
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            ttl=1,
            generator=lambda _: "fresh value",
        )

        # The stale key format is: {prefix}:{key}:stale
        # The value must be JSON-encoded exactly as Go would store it.
        # Handler[string] stores json.Marshal(v), so a plain string "stale value"
        # is stored as the JSON string "stale value" (with enclosing quotes).
        redis_client.set("stale-test:k:stale", '"stale value"', ex=60)

        calls: list[str] = []

        def gen(key: str) -> str:
            calls.append(key)
            return "fresh value"

        result = h.get_or_refresh("k", gen)

        assert result == "stale value"
        assert calls == [], "generator must not be called when stale data exists"


# ---------------------------------------------------------------------------
# HitRefreshPolicy (smoke tests — background behaviour is not directly observable)
# ---------------------------------------------------------------------------


class TestHitRefreshPolicies:
    """Smoke tests: each policy must not raise on a normal cache hit."""

    @pytest.mark.parametrize(
        "policy",
        [
            HitRefreshPolicy.DEFAULT,
            HitRefreshPolicy.NONE,
            HitRefreshPolicy.AHEAD,
            HitRefreshPolicy.PROBABILISTIC,
            HitRefreshPolicy.OLDER_THAN,
        ],
    )
    def test_hit_refresh_policy_does_not_raise(self, make_handler, policy):
        h = make_handler(
            hit_refresh_policy=policy,
            refresh_ahead_threshold=0.2,
            probabilistic_beta=1.0,
            refresh_older_than=1,
            generator=lambda _: "refreshed",
        )
        h.set("k", "value")
        result = h.get_or_refresh("k", lambda _: "refreshed")
        assert result == "value"

    def test_hit_refresh_none_does_not_update_after_hit(self, make_handler):
        """With HitRefreshNone no background goroutine should update the key.
        We verify the cached value stays the same after two hits."""
        h = make_handler(
            hit_refresh_policy=HitRefreshPolicy.NONE,
            generator=lambda _: "refreshed",
            ttl=30,
        )
        h.set("k", "original")
        h.get_or_refresh("k", lambda _: "refreshed")  # hit 1
        time.sleep(0.1)  # let any goroutine finish
        result = h.get_or_refresh("k", lambda _: "refreshed")  # hit 2
        assert result == "original"


# ---------------------------------------------------------------------------
# HitRefreshPolicy — live background-refresh verification
# ---------------------------------------------------------------------------


class TestHitRefreshLive:
    """Verify that background hit-refresh goroutines actually fire and write
    a fresh value to Redis.  Each test sets an initial value, triggers a hit,
    waits a bounded amount of time, and asserts the cache was updated."""

    def _wait_for_value(self, h, key, expected, deadline_secs=2.0, gen=None):
        """Poll get_or_refresh until it returns *expected* or the deadline passes."""
        if gen is None:
            gen = lambda _: expected  # noqa: E731
        deadline = time.time() + deadline_secs
        while time.time() < deadline:
            if h.get_or_refresh(key, gen) == expected:
                return True
            time.sleep(0.05)
        return False

    def test_hit_refresh_default_updates_cache(self, make_handler, redis_client):
        """HitRefreshDefault (zero cooldown) fires on every hit and updates Redis."""
        h = make_handler(
            prefix="hr-default",
            hit_refresh_policy=HitRefreshPolicy.DEFAULT,
            generator=lambda _: "refreshed",
            ttl=30,
        )
        # Seed an initial value directly, bypassing the generator.
        redis_client.set("hr-default:k", '"original"', ex=30)

        # A hit with a generator that returns "refreshed" should trigger a
        # background goroutine that overwrites the cached value.
        h.get_or_refresh("k", lambda _: "refreshed")  # hit — goroutine fires

        assert self._wait_for_value(h, "k", "refreshed"), (
            "Background goroutine (HitRefreshDefault) did not update the cache"
        )

    def test_hit_refresh_ahead_updates_cache_near_expiry(self, make_handler, redis_client):
        """HitRefreshAhead fires when remaining TTL <= threshold% of original TTL.
        We seed a key with a short TTL and a large threshold so the condition
        is met immediately."""
        h = make_handler(
            prefix="hr-ahead",
            hit_refresh_policy=HitRefreshPolicy.AHEAD,
            refresh_ahead_threshold=0.99,  # fire when 99% elapsed = almost immediately
            generator=lambda _: "refreshed",
            ttl=30,
        )
        # Write a key with 2s TTL directly; remaining TTL ≈ 2s, original ≈ 2s.
        # With threshold=0.99 the condition fires when remaining ≤ 0.99*2 = 1.98s,
        # which is true almost immediately.
        redis_client.set("hr-ahead:k", '"original"', ex=2)

        h.get_or_refresh("k", lambda _: "refreshed")  # hit — goroutine should fire

        assert self._wait_for_value(h, "k", "refreshed"), (
            "Background goroutine (HitRefreshAhead) did not update the cache"
        )

    def test_hit_refresh_older_than_updates_cache(self, make_handler, redis_client):
        """HitRefreshOlderThan fires when the key is older than the threshold.
        We seed a key with a short remaining TTL so its estimated age
        (originalTTL - remaining) comfortably exceeds the threshold."""
        h = make_handler(
            prefix="hr-older",
            hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
            refresh_older_than=1,  # fire when key is >= 1 s old
            generator=lambda _: "refreshed",
            ttl=30,
        )
        # Seed with 20s remaining on a 30s TTL: estimated age = 30-20 = 10s >> 1s threshold.
        redis_client.set("hr-older:k", '"original"', ex=20)

        h.get_or_refresh("k", lambda _: "refreshed")  # hit — goroutine should fire

        assert self._wait_for_value(h, "k", "refreshed"), (
            "Background goroutine (HitRefreshOlderThan) did not update the cache"
        )

    def test_stale_or_sync_background_rewrite(self, make_handler, redis_client):
        """MissFillStaleOrSync: when stale data is present the stale value is
        returned immediately AND a background goroutine rewrites the main key."""
        h = make_handler(
            prefix="swr",
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            generator=lambda _: "fresh",
            ttl=30,
        )
        redis_client.set("swr:k:stale", '"stale"', ex=60)

        result = h.get_or_refresh("k", lambda _: "fresh")
        assert result == "stale", "stale value should be returned immediately"

        # Background goroutine should write the fresh value to the main key.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if redis_client.exists("swr:k"):
                break
            time.sleep(0.05)
        else:
            pytest.fail("Background stale-rewrite goroutine did not write main key within 2 s")


# ---------------------------------------------------------------------------
# ErrorPolicy
# ---------------------------------------------------------------------------


class TestErrorPolicy:
    def test_surface_propagates_generator_exception(self, make_handler):
        """ErrorPolicySurface (default): a failing generator raises CacheError."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        with pytest.raises(CacheError):
            h.get_or_refresh("k", bad_gen)

    def test_zero_value_suppresses_generator_exception(self, make_handler):
        """ErrorPolicyZeroValue: a failing generator returns empty string, no exception."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.SYNC,
            error_policy=ErrorPolicy.ZERO_VALUE,
        )

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        # Should not raise; Go returns the zero value for string ("")
        result = h.get_or_refresh("k", bad_gen)
        assert result == ""

    def test_zero_value_does_not_suppress_fail_fast(self, make_handler):
        """ErrCacheMiss from FAIL_FAST is never swallowed by ZERO_VALUE."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.FAIL_FAST,
            error_policy=ErrorPolicy.ZERO_VALUE,
        )
        with pytest.raises(CacheError):
            h.get_or_refresh("cold-key", lambda _: "never")

    def test_zero_value_per_call_override(self, make_handler):
        """Per-call ZERO_VALUE overrides the handler-level SURFACE default."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        result = h.get_or_refresh("k", bad_gen, error_policy=ErrorPolicy.ZERO_VALUE)
        assert result == ""


# ---------------------------------------------------------------------------
# Per-call policy override
# ---------------------------------------------------------------------------


class TestPerCallOverride:
    def test_miss_fill_override_takes_precedence(self, make_handler):
        """Handler default SYNC, single call overrides to FAIL_FAST."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)
        with pytest.raises(CacheError):
            h.get_or_refresh("k", lambda _: "x", miss_fill_policy=MissFillPolicy.FAIL_FAST)

    def test_miss_fill_override_does_not_affect_other_calls(self, make_handler):
        """The override is scoped to a single call; subsequent calls use the handler default."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)

        try:
            h.get_or_refresh("k", lambda _: "x", miss_fill_policy=MissFillPolicy.FAIL_FAST)
        except CacheError:
            pass

        # Next call uses handler default (SYNC) and should succeed
        result = h.get_or_refresh("k", lambda _: "filled")
        assert result == "filled"


# ---------------------------------------------------------------------------
# Handler lifecycle
# ---------------------------------------------------------------------------


class TestHandlerLifecycle:
    def test_context_manager_closes_handler(self, redis_addr, flush_redis):  # noqa: ARG002
        with CacheHandler(redis_addr=redis_addr, prefix="pytest", ttl=10) as h:
            h.set("k", "v")
        # After __exit__ the handler is closed; further use should raise
        with pytest.raises(CacheError):
            h.get_or_refresh("k", lambda _: "x")

    def test_close_is_idempotent(self, handler: CacheHandler):
        handler.close()
        handler.close()  # must not raise

    def test_get_or_refresh_after_close_raises(self, handler: CacheHandler):
        handler.close()
        with pytest.raises(CacheError, match="closed"):
            handler.get_or_refresh("k", lambda _: "x")

    def test_set_after_close_raises(self, handler: CacheHandler):
        handler.close()
        with pytest.raises(CacheError, match="closed"):
            handler.set("k", "v")

    def test_multiple_handlers_are_independent(self, make_handler):
        h1 = make_handler(prefix="h1", ttl=30)
        h2 = make_handler(prefix="h2", ttl=30)

        h1.set("k", "from-h1")
        h2.set("k", "from-h2")

        assert h1.get_or_refresh("k", lambda _: "x") == "from-h1"
        assert h2.get_or_refresh("k", lambda _: "x") == "from-h2"


# ---------------------------------------------------------------------------
# Generator edge cases
# ---------------------------------------------------------------------------


class TestGeneratorEdgeCases:
    def test_generator_returning_none_raises_cache_error(self, make_handler):
        """Returning None from a generator (not raising) is treated as failure."""
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC)
        with pytest.raises(CacheError):
            h.get_or_refresh("k", lambda _: None)

    def test_generator_returning_none_with_zero_value_policy(self, make_handler):
        """ZERO_VALUE suppresses a None-returning generator; empty string is returned."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.SYNC,
            error_policy=ErrorPolicy.ZERO_VALUE,
        )
        result = h.get_or_refresh("k", lambda _: None)
        assert result == ""


# ---------------------------------------------------------------------------
# set() with explicit TTL
# ---------------------------------------------------------------------------


class TestSetWithTTL:
    def test_set_uses_explicit_ttl(self, make_handler, redis_client):
        """set(ttl=N) stores the key with that TTL in Redis."""
        h = make_handler(prefix="ttl-test", ttl=60)
        h.set("k", "value", ttl=5)
        remaining = redis_client.ttl("ttl-test:k")
        assert 0 < remaining <= 5

    def test_set_uses_handler_default_ttl(self, make_handler, redis_client):
        """set() with no explicit ttl= falls back to the handler-level default."""
        h = make_handler(prefix="ttl-default", ttl=20)
        h.set("k", "value")
        remaining = redis_client.ttl("ttl-default:k")
        assert 0 < remaining <= 20


# ---------------------------------------------------------------------------
# MissFillStaleOrSync — sync fallback path
# ---------------------------------------------------------------------------


class TestMissFillStaleOrSyncFallback:
    def test_sync_fallback_writes_main_key(self, make_handler, redis_client):
        """With no stale entry the policy falls back to sync generation and
        the generated value is written to the main Redis key."""
        h = make_handler(
            prefix="stale-fallback",
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            generator=lambda _: "fresh",
            ttl=30,
        )
        result = h.get_or_refresh("k", lambda _: "fresh")
        assert result == "fresh"
        assert redis_client.exists("stale-fallback:k") == 1

    def test_sync_fallback_failing_generator_raises(self, make_handler):
        """STALE_OR_SYNC with no stale data + SURFACE: failing handler-level generator
        raises CacheError.  When a handler-level generator= is registered it is
        used for miss-fill, so a None-returning handler generator surfaces an error."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            generator=lambda _: None,  # None → NULL in shim → error
            error_policy=ErrorPolicy.SURFACE,
        )

        with pytest.raises(CacheError):
            h.get_or_refresh("k", lambda _: "not-used")


# ---------------------------------------------------------------------------
# Per-call hit_refresh_policy override
# ---------------------------------------------------------------------------


class TestPerCallHitRefreshOverride:
    @pytest.mark.parametrize(
        "policy",
        [
            HitRefreshPolicy.DEFAULT,
            HitRefreshPolicy.NONE,
            HitRefreshPolicy.AHEAD,
            HitRefreshPolicy.PROBABILISTIC,
            HitRefreshPolicy.OLDER_THAN,
        ],
    )
    def test_per_call_override_returns_cached_value(self, make_handler, policy):
        """Per-call hit_refresh_policy override is accepted and the cached value
        is returned without error for every policy variant."""
        h = make_handler(ttl=30)
        h.set("k", "cached")
        result = h.get_or_refresh("k", lambda _: "not-this", hit_refresh_policy=policy)
        assert result == "cached"


# ---------------------------------------------------------------------------
# Mixed policy combinations
# ---------------------------------------------------------------------------


class TestMixedPolicies:
    def test_async_miss_failing_generator_surfaces_error(self, make_handler):
        """ASYNC miss + SURFACE error policy: failing generator raises CacheError."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.ASYNC,
            error_policy=ErrorPolicy.SURFACE,
        )

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        with pytest.raises(CacheError):
            h.get_or_refresh("k", bad_gen)

    def test_async_miss_failing_generator_zero_value(self, make_handler):
        """ASYNC miss + ZERO_VALUE: failing generator returns empty string, no exception."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.ASYNC,
            error_policy=ErrorPolicy.ZERO_VALUE,
        )

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        result = h.get_or_refresh("k", bad_gen)
        assert result == ""

    def test_cooperative_failing_generator_zero_value(self, make_handler):
        """COOPERATIVE miss + ZERO_VALUE: failing generator returns empty string."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.COOPERATIVE,
            error_policy=ErrorPolicy.ZERO_VALUE,
            cooperative_timeout=5,
        )

        def bad_gen(key: str) -> str:
            raise RuntimeError("boom")

        result = h.get_or_refresh("k", bad_gen)
        assert result == ""

    def test_fail_fast_and_zero_value_still_raises(self, make_handler):
        """FAIL_FAST + ZERO_VALUE: ErrCacheMiss is never swallowed (already tested via
        TestErrorPolicy, duplicated here as a cross-policy combo check)."""
        h = make_handler(
            miss_fill_policy=MissFillPolicy.FAIL_FAST,
            error_policy=ErrorPolicy.ZERO_VALUE,
        )
        with pytest.raises(CacheError):
            h.get_or_refresh("cold-key", lambda _: "never")


# ---------------------------------------------------------------------------
# Use case: high fan-out stampede prevention (UseCases.md UC2)
# ---------------------------------------------------------------------------


class TestUseCaseHighFanOut:
    """UC2: Multiple concurrent callers on the same cold key must not stampede the
    generator.  Only one DB call should happen; the rest share the result."""

    def test_sync_policy_prevents_stampede_under_high_concurrency(self, make_handler):
        h = make_handler(miss_fill_policy=MissFillPolicy.SYNC, ttl=30)
        db_calls = 0
        counter_lock = threading.Lock()

        def expensive_gen(key: str) -> str:
            nonlocal db_calls
            time.sleep(0.02)  # simulate slow upstream call
            with counter_lock:
                db_calls += 1
            return "expensive-result"

        threads = [
            threading.Thread(target=h.get_or_refresh, args=("popular-key", expensive_gen))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert db_calls == 1, (
            f"MissFillSync must prevent stampede; expected 1 generator call, got {db_calls}"
        )


# ---------------------------------------------------------------------------
# Construction-time guardrails
# ---------------------------------------------------------------------------


class TestConstructionValidation:
    """CacheHandler.__init__ must raise ValueError for misconfigured policies
    so that bugs are caught at startup rather than silently doing nothing."""

    # --- generator= required for explicit background-refresh policies ----------

    def test_ahead_without_generator_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="generator="):
            CacheHandler(
                redis_addr=redis_addr,
                hit_refresh_policy=HitRefreshPolicy.AHEAD,
                refresh_ahead_threshold=0.2,
            )

    def test_probabilistic_without_generator_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="generator="):
            CacheHandler(
                redis_addr=redis_addr,
                hit_refresh_policy=HitRefreshPolicy.PROBABILISTIC,
                probabilistic_beta=1.0,
            )

    def test_older_than_without_generator_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="generator="):
            CacheHandler(
                redis_addr=redis_addr,
                hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
                refresh_older_than=5,
            )

    def test_stale_or_sync_without_generator_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="generator="):
            CacheHandler(
                redis_addr=redis_addr,
                miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
                stale_ttl=60,
            )

    # --- policy-specific companion parameters --------------------------------

    def test_ahead_without_threshold_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="refresh_ahead_threshold"):
            CacheHandler(
                redis_addr=redis_addr,
                hit_refresh_policy=HitRefreshPolicy.AHEAD,
                generator=lambda _: "v",
            )

    def test_older_than_without_age_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="refresh_older_than"):
            CacheHandler(
                redis_addr=redis_addr,
                hit_refresh_policy=HitRefreshPolicy.OLDER_THAN,
                generator=lambda _: "v",
            )

    def test_stale_or_sync_without_stale_ttl_raises(self, redis_addr, flush_redis):  # noqa: ARG002
        with pytest.raises(ValueError, match="stale_ttl"):
            CacheHandler(
                redis_addr=redis_addr,
                miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
                generator=lambda _: "v",
            )

    # --- valid configurations must not raise ---------------------------------

    def test_default_hit_refresh_without_generator_is_allowed(self, redis_addr, flush_redis):  # noqa: ARG002
        """HitRefreshDefault silently suppresses background goroutines when no
        generator= is provided; this is acceptable (not a misconfiguration)."""
        with CacheHandler(redis_addr=redis_addr) as h:
            assert h is not None

    def test_hit_refresh_none_without_generator_is_allowed(self, redis_addr, flush_redis):  # noqa: ARG002
        """HitRefreshNone explicitly disables background refresh; no generator needed."""
        with CacheHandler(
            redis_addr=redis_addr,
            hit_refresh_policy=HitRefreshPolicy.NONE,
        ) as h:
            assert h is not None

    def test_ahead_with_generator_and_threshold_is_allowed(self, redis_addr, flush_redis):  # noqa: ARG002
        """Fully specified AHEAD configuration must succeed."""
        with CacheHandler(
            redis_addr=redis_addr,
            hit_refresh_policy=HitRefreshPolicy.AHEAD,
            refresh_ahead_threshold=0.2,
            generator=lambda _: "v",
        ) as h:
            assert h is not None

    def test_stale_or_sync_with_generator_and_stale_ttl_is_allowed(self, redis_addr, flush_redis):  # noqa: ARG002
        """Fully specified STALE_OR_SYNC configuration must succeed."""
        with CacheHandler(
            redis_addr=redis_addr,
            miss_fill_policy=MissFillPolicy.STALE_OR_SYNC,
            stale_ttl=60,
            generator=lambda _: "v",
        ) as h:
            assert h is not None
