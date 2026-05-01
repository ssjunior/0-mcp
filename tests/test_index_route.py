"""The index route registered by `get_routes` should respond at the
include's empty path (e.g. `/api/`)."""
import pytest
from django.test import AsyncRequestFactory

from zeromcp.base import BaseResource
from zeromcp.routes import get_routes
from tests.testapp.models import Space


class _SpaceResource(BaseResource):
    model = Space
    summary = 'space'
    description = 'Test resource.'
    authenticated = False


def test_get_routes_registers_index_at_empty_path():
    routes = get_routes({'spaces(.*)$': _SpaceResource}, docs_public=True)
    paths = [getattr(r, 'pattern', None) for r in routes]
    pattern_strs = [str(p) for p in paths]
    assert '' in pattern_strs, f'index route missing; got {pattern_strs}'


def test_get_routes_has_no_duplicate_empty_paths():
    routes = get_routes({'spaces(.*)$': _SpaceResource}, docs_public=True)
    empties = [r for r in routes if str(getattr(r, 'pattern', '')) == '']
    assert len(empties) == 1


@pytest.mark.asyncio
async def test_index_renders_html_with_docs_links():
    """Index page lists the four entry points: ``/docs``, ``/openapi.json``,
    ``/mcp/tools``, ``/mcp/tools.json``. The per-resource list was
    intentionally dropped — it duplicated what those pages already show
    and grew unwieldy on projects with many resources."""
    routes = get_routes({'spaces(.*)$': _SpaceResource}, docs_public=True, mcp=True)
    index_route = next(r for r in routes if str(getattr(r, 'pattern', '')) == '')
    factory = AsyncRequestFactory()
    request = factory.get('/')
    response = await index_route.callback(request)
    assert response.status_code == 200
    body = response.content.decode('utf-8')
    assert '/docs' in body
    assert '/openapi.json' in body
    assert '/mcp/tools' in body
    assert '/mcp/tools.json' in body
