"""Index, /docs and /openapi.json must be gated when docs_public=False."""
import pytest
from django.test import RequestFactory

from zeromcp.base import BaseResource
from zeromcp.routes import get_routes
from tests.testapp.models import Space


class _SpaceResource(BaseResource):
    model = Space
    list_fields = ['id', 'name']
    authenticated = False


ENDPOINTS = {'spaces(.*)$': _SpaceResource}


def _route_handler(routes, suffix):
    """Find the handler attached to the URL pattern ending with ``suffix``."""
    for route in routes:
        pattern = str(route.pattern)
        if pattern == suffix or pattern.endswith(suffix):
            return route.callback
    raise AssertionError(f'route {suffix!r} not found')


def _index_handler(routes):
    # the index is registered as path('') — its pattern stringifies to ''
    for route in routes:
        if str(route.pattern) == '':
            return route.callback
    raise AssertionError('index route not found')


@pytest.mark.asyncio
async def test_index_returns_401_when_private_and_no_session():
    routes = get_routes(ENDPOINTS, docs_public=False)
    handler = _index_handler(routes)
    request = RequestFactory().get('/')
    response = await handler(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_index_returns_200_when_public():
    routes = get_routes(ENDPOINTS, docs_public=True)
    handler = _index_handler(routes)
    request = RequestFactory().get('/')
    response = await handler(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_openapi_json_returns_401_when_private_and_no_session():
    routes = get_routes(ENDPOINTS, docs_public=False)
    handler = _route_handler(routes, 'openapi.json')
    request = RequestFactory().get('/openapi.json')
    response = await handler(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_docs_returns_401_when_private_and_no_session():
    routes = get_routes(ENDPOINTS, docs_public=False)
    handler = _route_handler(routes, 'docs')
    request = RequestFactory().get('/docs')
    response = await handler(request)
    assert response.status_code == 401
