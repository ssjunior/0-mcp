import pytest

from zeromcp import security
from zeromcp.blocking import block_key
from zeromcp.redis_config import KEY_PREFIX, get_redis
from zeromcp.security import SecurityMiddleware, _block_key, _4xx_key


class FakeRequest:
    def __init__(self, path='/api/x', qs='', ua='curl/8.0', remote='1.1.1.1'):
        self.path = path
        self.META = {
            'REMOTE_ADDR': remote,
            'QUERY_STRING': qs,
            'HTTP_USER_AGENT': ua,
        }


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def make_middleware(response_status=200):
    async def get_response(request):
        return FakeResponse(response_status)
    return SecurityMiddleware(get_response)


@pytest.mark.asyncio
async def test_passes_clean_request(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(path='/api/users'))
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_blocks_scanner_path(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(path='/.env'))
    assert res.status_code == 403
    assert await fake_redis.get(block_key('1.1.1.1')) is not None


@pytest.mark.asyncio
async def test_blocks_wp_admin(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(path='/wp-admin/setup-config.php'))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_blocks_path_traversal(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(path='/api/../../etc/passwd'))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_blocks_sqli_in_qs(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(path='/api/users', qs="id=1' or 1=1"))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_blocks_known_scanner_ua(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(ua='sqlmap/1.0'))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_blocks_nikto(fake_redis):
    mw = make_middleware()
    res = await mw(FakeRequest(ua='Mozilla/5.0 nikto'))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_already_blocked_short_circuits(fake_redis):
    redis = get_redis()
    await redis.setex(block_key('1.1.1.1'), 60, 'prior')

    mw = make_middleware(response_status=200)
    res = await mw(FakeRequest(path='/api/clean'))
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_4xx_burst_triggers_block(fake_redis, monkeypatch):
    monkeypatch.setattr(security, 'MAX_4XX_PER_MINUTE', 3)
    monkeypatch.setattr(security, 'MAX_4XX_PER_HOUR', 999)

    mw = make_middleware(response_status=401)

    for _ in range(3):
        res = await mw(FakeRequest(path='/api/x', remote='2.2.2.2'))
        assert res.status_code == 401

    res = await mw(FakeRequest(path='/api/x', remote='2.2.2.2'))
    assert res.status_code == 403
    assert await fake_redis.get(block_key('2.2.2.2')) is not None


@pytest.mark.asyncio
async def test_4xx_slow_probe_triggers_block(fake_redis, monkeypatch):
    """Even under the per-minute threshold, repeated 4xx over time blocks."""
    monkeypatch.setattr(security, 'MAX_4XX_PER_MINUTE', 999)
    monkeypatch.setattr(security, 'MAX_4XX_PER_HOUR', 4)

    mw = make_middleware(response_status=404)

    for _ in range(4):
        res = await mw(FakeRequest(path='/api/probe', remote='5.5.5.5'))
        assert res.status_code == 404

    res = await mw(FakeRequest(path='/api/probe', remote='5.5.5.5'))
    assert res.status_code == 403
    assert await fake_redis.get(block_key('5.5.5.5')) is not None


@pytest.mark.asyncio
async def test_4xx_429_does_not_count(fake_redis, monkeypatch):
    monkeypatch.setattr(security, 'MAX_4XX_PER_MINUTE', 1)

    mw = make_middleware(response_status=429)
    await mw(FakeRequest(remote='3.3.3.3'))
    await mw(FakeRequest(remote='3.3.3.3'))
    assert await fake_redis.get(block_key('3.3.3.3')) is None


@pytest.mark.asyncio
async def test_2xx_does_not_count(fake_redis, monkeypatch):
    monkeypatch.setattr(security, 'MAX_4XX_PER_MINUTE', 1)

    mw = make_middleware(response_status=200)
    for _ in range(5):
        await mw(FakeRequest(remote='4.4.4.4'))
    assert await fake_redis.get(block_key('4.4.4.4')) is None
    assert await fake_redis.get(_4xx_key('4.4.4.4')) is None
