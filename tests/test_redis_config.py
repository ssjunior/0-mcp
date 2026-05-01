import os
import subprocess
import sys

import pytest

from zeromcp import redis_config
from zeromcp.redis_config import (
    KEY_PREFIX,
    REDIS_PREFIX,
    get_cache_stats,
    get_redis,
    reset_cache_stats,
)


def test_key_prefix_built():
    assert REDIS_PREFIX == 'test'
    assert KEY_PREFIX == 'test:'


def test_get_redis_returns_singleton(fake_redis):
    a = get_redis()
    b = get_redis()
    assert a is b


def test_redis_env_is_not_parsed_at_import_time():
    env = os.environ.copy()
    env['PYTHONPATH'] = 'tests'
    env['REDIS_SERVER'] = 'localhost'
    env['REDIS_DB'] = 'not-an-int'

    result = subprocess.run(
        [sys.executable, '-c', 'import zeromcp.redis_config; print("ok")'],
        cwd=os.getcwd(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'ok'


def test_get_redis_caches_per_loop(monkeypatch):
    """Without the test injection hook, get_redis must hand a separate
    client to each event loop — otherwise asyncio raises 'Future
    attached to a different loop' under ASGI + async_to_sync."""
    import asyncio

    monkeypatch.setattr(redis_config, '_redis_client', None)
    redis_config._clients.clear()

    async def grab():
        return get_redis()

    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        client_a = loop_a.run_until_complete(grab())
        client_b = loop_b.run_until_complete(grab())
        # Same loop reused → same client; different loops → different clients.
        client_a_again = loop_a.run_until_complete(grab())
        assert client_a is client_a_again
        assert client_a is not client_b
    finally:
        loop_a.close()
        loop_b.close()


def test_get_redis_test_hook_overrides_per_loop_cache(fake_redis):
    """The conftest fixture monkeypatches ``_redis_client``; that hook
    must short-circuit the per-loop logic so tests get the fakeredis
    instance regardless of which loop they run in."""
    a = get_redis()
    b = get_redis()
    assert a is b is fake_redis


@pytest.mark.asyncio
async def test_cache_stats_empty(fake_redis):
    await reset_cache_stats()
    stats = await get_cache_stats()
    assert stats == {
        'hits': 0,
        'misses': 0,
        'total': 0,
        'ratio': 0.0,
        'by_model': {},
    }


@pytest.mark.asyncio
async def test_cache_stats_counts(fake_redis):
    await reset_cache_stats()
    redis = get_redis()
    await redis.incr(f'{KEY_PREFIX}cache_stats:hits')
    await redis.incr(f'{KEY_PREFIX}cache_stats:hits')
    await redis.incr(f'{KEY_PREFIX}cache_stats:hits')
    await redis.incr(f'{KEY_PREFIX}cache_stats:misses')
    # Per-model counters live in a single hash now (HGETALL on read,
    # HINCRBY on write) so ``get_cache_stats`` doesn't SCAN the whole
    # keyspace when Redis has millions of unrelated keys.
    by_model_key = f'{KEY_PREFIX}cache_stats:by_model'
    await redis.hincrby(by_model_key, 'hits:testapp.space', 1)
    await redis.hincrby(by_model_key, 'misses:testapp.space', 1)

    stats = await get_cache_stats()
    assert stats['hits'] == 3
    assert stats['misses'] == 1
    assert stats['total'] == 4
    assert stats['ratio'] == 0.75
    assert stats['by_model']['testapp.space'] == {
        'hits': 1,
        'misses': 1,
        'total': 2,
        'ratio': 0.5,
    }


@pytest.mark.asyncio
async def test_cache_stats_handles_only_misses(fake_redis):
    """Resource that always misses should still appear with hits=0."""
    await reset_cache_stats()
    redis = get_redis()
    by_model_key = f'{KEY_PREFIX}cache_stats:by_model'
    await redis.hincrby(by_model_key, 'misses:testapp.space', 5)

    stats = await get_cache_stats()
    assert stats['by_model']['testapp.space'] == {
        'hits': 0,
        'misses': 5,
        'total': 5,
        'ratio': 0.0,
    }


@pytest.mark.asyncio
async def test_reset_cache_stats(fake_redis):
    redis = get_redis()
    by_model_key = f'{KEY_PREFIX}cache_stats:by_model'
    await redis.incr(f'{KEY_PREFIX}cache_stats:hits')
    await redis.hincrby(by_model_key, 'misses:testapp.space', 3)

    await reset_cache_stats()

    stats = await get_cache_stats()
    assert stats['hits'] == 0
    assert stats['misses'] == 0
    assert stats['by_model'] == {}
