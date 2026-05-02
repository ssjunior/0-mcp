"""Global ``READ_ONLY`` gate — every non-GET request returns 405,
regardless of per-resource ``allowed_methods``."""
import pytest
from settings import settings as project_settings

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


class WritableResource(BaseResource):
    model = Space
    authenticated = False

    async def get(self, request):
        return {'ok': True}

    async def post(self, request):
        return {'created': True}


class _FakeGet:
    def get(self, key, default=None):
        return default

    def urlencode(self):
        return ''

    def __iter__(self):
        return iter([])

    def __contains__(self, key):
        return False


class FakeRequest:
    path = '/api/test'
    path_info = '/api/test'
    body = b'{}'
    content_type = 'application/json'
    headers = {}
    COOKIES = {}

    def __init__(self, method='GET'):
        self.method = method
        self.GET = _FakeGet()
        self.META = {'REMOTE_ADDR': '127.0.0.1'}


@pytest.mark.asyncio
async def test_read_only_blocks_post(monkeypatch, fake_redis):
    monkeypatch.setattr(
        project_settings, 'MCP', raising=False, value={'READ_ONLY': True},
    )
    res = WritableResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(FakeRequest(method='POST'))
    assert exc.value.args[0] == 405
    assert 'READ_ONLY' in exc.value.args[1]


@pytest.mark.asyncio
async def test_read_only_allows_get(monkeypatch, fake_redis):
    monkeypatch.setattr(
        project_settings, 'MCP', raising=False, value={'READ_ONLY': True},
    )
    res = WritableResource()
    response = await res.dispatch(FakeRequest(method='GET'))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_read_only_default_off_post_passes(monkeypatch, fake_redis):
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={})
    res = WritableResource()
    response = await res.dispatch(FakeRequest(method='POST'))
    assert response.status_code == 200
