"""Friendly error when Redis < 6.2 returns ``unknown command 'GETEX'``.

Older Redis builds (default on RHEL/Oracle Linux 8 AppStream pre-`redis:6`)
don't have ``GETEX``. The raw ``ResponseError`` is cryptic — users spend
hours diagnosing. We translate it once into an actionable ``RuntimeError``
the first time it surfaces."""
import pytest
from redis.exceptions import ResponseError

from zeromcp.redis_config import getex


class _OldRedis:
    async def getex(self, key, ex):
        raise ResponseError(
            "unknown command 'GETEX', with args beginning with: 'sessions:abc' 'EX' '1800'",
        )


class _ModernRedis:
    async def getex(self, key, ex):
        return b'{"user":{"id":1}}'


class _ErrorRedis:
    async def getex(self, key, ex):
        raise ResponseError('something else broke')


@pytest.mark.asyncio
async def test_translates_unknown_getex_to_friendly_runtime_error():
    with pytest.raises(RuntimeError) as exc:
        await getex(_OldRedis(), 'k', ex=60)
    msg = str(exc.value)
    assert 'Redis 6.2' in msg
    assert 'GETEX' in msg
    assert 'dnf module enable redis:6' in msg


@pytest.mark.asyncio
async def test_passes_through_modern_redis_response():
    result = await getex(_ModernRedis(), 'k', ex=60)
    assert result == b'{"user":{"id":1}}'


@pytest.mark.asyncio
async def test_unrelated_response_error_propagates_unchanged():
    with pytest.raises(ResponseError) as exc:
        await getex(_ErrorRedis(), 'k', ex=60)
    assert 'something else broke' in str(exc.value)
