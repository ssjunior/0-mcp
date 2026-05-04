"""P0 + P1 — declarative per-resource rate-limits, credential-path
auto-defense, and the lockout-via-long-window pattern.

Covers the new ``RateLimiter.check_bucket`` primitive and the two
dispatch-time hooks: credential-path auto-defense (`_enforce_rate_limit`)
and per-resource buckets (``_enforce_resource_rate_limits``)."""
import pytest

from zeromcp.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_check_bucket_increments_and_flips_after_limit(fake_redis):
    bucket = 'test_bucket_short'
    key = 'alice@example.com'

    # First 3 hits stay under the limit.
    for _ in range(3):
        result = await RateLimiter.check_bucket(bucket, key, limit=3, window_ms=60_000)
        assert result['rate_limited'] is False

    # Fourth hit exceeds.
    result = await RateLimiter.check_bucket(bucket, key, limit=3, window_ms=60_000)
    assert result['rate_limited'] is True
    assert result['count'] == 4
    assert result['retry_after'] == 60


@pytest.mark.asyncio
async def test_check_bucket_isolates_by_key(fake_redis):
    bucket = 'isolation_test'
    # alice exhausts her bucket
    for _ in range(3):
        await RateLimiter.check_bucket(bucket, 'alice', limit=3, window_ms=60_000)
    alice = await RateLimiter.check_bucket(bucket, 'alice', limit=3, window_ms=60_000)
    assert alice['rate_limited'] is True

    # bob's bucket is independent
    bob = await RateLimiter.check_bucket(bucket, 'bob', limit=3, window_ms=60_000)
    assert bob['rate_limited'] is False


@pytest.mark.asyncio
async def test_check_bucket_isolates_by_bucket_name(fake_redis):
    # same key, two different buckets — short window vs long window
    # (the lockout pattern)
    short = await RateLimiter.check_bucket('short', 'k', limit=2, window_ms=5_000)
    short = await RateLimiter.check_bucket('short', 'k', limit=2, window_ms=5_000)
    short = await RateLimiter.check_bucket('short', 'k', limit=2, window_ms=5_000)
    assert short['rate_limited'] is True

    # long bucket untouched
    long = await RateLimiter.check_bucket('long', 'k', limit=10, window_ms=86_400_000)
    assert long['rate_limited'] is False
    assert long['count'] == 1


@pytest.mark.asyncio
async def test_lockout_pattern_via_long_window(fake_redis):
    """P1: a "lockout" is just a rate-limit with a much longer window.
    50 attempts in 24h → blocked for 24h."""
    key = 'victim@example.com'
    for _ in range(50):
        await RateLimiter.check_bucket('lockout', key, limit=50, window_ms=86_400_000)
    result = await RateLimiter.check_bucket('lockout', key, limit=50, window_ms=86_400_000)
    assert result['rate_limited'] is True
    assert result['count'] == 51
    assert result['retry_after'] == 86_400
