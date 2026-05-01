import pytest

from zeromcp.base import BaseResource
from zeromcp.blocking import block, block_key
from zeromcp.exception import HTTPException
from zeromcp.rate_limit import RateLimiter
from zeromcp.redis_config import get_redis
from tests.testapp.models import Space


class Res(BaseResource):
    model = Space


@pytest.mark.asyncio
async def test_check_is_blocked_no_key(fake_redis):
    res = Res()
    await res.check_is_blocked('1.2.3.4')


@pytest.mark.asyncio
async def test_check_is_blocked_blocked(fake_redis):
    redis = get_redis()
    await redis.set(block_key('1.2.3.4'), '1')
    res = Res()
    with pytest.raises(HTTPException) as exc:
        await res.check_is_blocked('1.2.3.4')
    assert exc.value.args[0] == 403


@pytest.mark.asyncio
async def test_block_sets_key_with_ttl(fake_redis):
    res = Res()
    with pytest.raises(HTTPException):
        await res.block('5.6.7.8')

    redis = get_redis()
    key = block_key('5.6.7.8')
    assert await redis.get(key) == 'api_rate_limit'
    ttl = await redis.ttl(key)
    assert 0 < ttl <= 86400


@pytest.mark.asyncio
async def test_rate_limiter_uses_shared_block_namespace(fake_redis):
    redis = get_redis()
    await block(redis, '9.9.9.9', 'manual', ttl=60)

    assert await RateLimiter.is_blocked('9.9.9.9') is True


@pytest.mark.asyncio
async def test_rate_limiter_track_failure_writes_shared_block_namespace(fake_redis):
    await RateLimiter.track_failure(
        '8.8.8.8', action='login', max_attempts=2, block_duration=60_000,
    )
    result = await RateLimiter.track_failure(
        '8.8.8.8', action='login', max_attempts=2, block_duration=60_000,
    )

    redis = get_redis()
    assert result['blocked'] is True
    assert await redis.get(block_key('8.8.8.8')) == 'login-failures'
