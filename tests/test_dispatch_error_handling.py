import json

import pytest
from django.test import override_settings

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


class CrashingResource(BaseResource):
    model = Space
    authenticated = False

    async def get(self, request):
        raise KeyError('HTTP_REFERER')


class HTTPExceptionResource(BaseResource):
    model = Space
    authenticated = False

    async def get(self, request):
        raise HTTPException(404, 'Not found')


class FakeRequest:
    method = 'GET'
    path = '/api/test'
    path_info = '/api/test'
    body = b''
    content_type = 'application/json'
    headers = {}
    COOKIES = {}

    def __init__(self):
        self.GET = _FakeGet()
        self.META = {
            'REMOTE_ADDR': '127.0.0.1',
        }


class _FakeGet:
    def get(self, key, default=None):
        return default

    def urlencode(self):
        return ''

    def __iter__(self):
        return iter([])

    def __contains__(self, key):
        return False


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_unhandled_error_returns_sanitized_500_in_prod(fake_redis):
    res = CrashingResource()
    response = await res.dispatch(FakeRequest())
    assert response.status_code == 500
    body = json.loads(response.content)
    assert body['success'] is False
    assert body['status'] == 500
    assert body['detail'] == 'Internal error'
    assert 'KeyError' not in response.content.decode()
    assert 'HTTP_REFERER' not in response.content.decode()


@pytest.mark.asyncio
@override_settings(DEBUG=True)
async def test_unhandled_error_reraises_in_debug(fake_redis):
    res = CrashingResource()
    with pytest.raises(KeyError):
        await res.dispatch(FakeRequest())


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_http_exception_passes_through(fake_redis):
    res = HTTPExceptionResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(FakeRequest())
    assert exc.value.args[0] == 404
