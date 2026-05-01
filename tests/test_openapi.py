"""Coverage for OpenAPI 3.1 spec generation."""
from pydantic import BaseModel

from zeromcp.base import BaseResource
from zeromcp.openapi import build_spec
from zeromcp.schemas import openapi
from tests.testapp.models import Space, User


class SpaceCreate(BaseModel):
    name: str
    description: str = ''


class SpaceOut(BaseModel):
    id: int
    name: str


class SpaceResource(BaseResource):
    model = Space
    allowed_methods = ['get', 'post', 'patch', 'delete']
    create_schema = SpaceCreate
    update_schema = SpaceCreate
    list_schema = SpaceOut


class UserResource(BaseResource):
    model = User
    allowed_methods = ['get', 'post']
    create_fields = ['email', 'password']
    list_fields = ['id', 'email']


def test_spec_basic_structure():
    spec = build_spec({'spaces(.*)$': SpaceResource})
    assert spec['openapi'].startswith('3.')
    assert 'paths' in spec
    assert 'components' in spec


def test_spec_pydantic_schema_referenced():
    spec = build_spec({'spaces(.*)$': SpaceResource})
    schemas = spec['components']['schemas']
    assert 'SpaceCreate' in schemas
    assert 'SpaceOut' in schemas


def test_spec_includes_security_schemes():
    spec = build_spec({'spaces(.*)$': SpaceResource})
    schemes = spec['components']['securitySchemes']
    assert 'cookieAuth' in schemes
    assert 'apiKeyAuth' in schemes


def test_cookie_auth_uses_configured_cookie_id():
    """The OpenAPI spec must reflect the project's actual COOKIE_ID,
    not a hardcoded ``sessionid`` — otherwise codegen and try-it-out
    in Scalar disagree with the running API."""
    from settings.settings import MCP

    spec = build_spec({'spaces(.*)$': SpaceResource})
    cookie_scheme = spec['components']['securitySchemes']['cookieAuth']
    assert cookie_scheme['name'] == MCP['COOKIE_ID']


def test_spec_creates_list_and_detail_paths():
    spec = build_spec({'spaces(.*)$': SpaceResource})
    paths = spec['paths']
    assert '/spaces' in paths
    assert '/spaces/{id}' in paths
    assert 'get' in paths['/spaces']
    assert 'post' in paths['/spaces']
    assert 'get' in paths['/spaces/{id}']
    assert 'patch' in paths['/spaces/{id}']
    assert 'delete' in paths['/spaces/{id}']


def test_spec_fallback_to_django_introspection():
    spec = build_spec({'users(.*)$': UserResource})
    # No Pydantic schema set; body should be inline introspected schema
    body = spec['paths']['/users']['post']['requestBody']
    schema = body['content']['application/json']['schema']
    assert schema.get('type') == 'object'
    assert 'email' in schema['properties']


def test_spec_method_filtering():
    spec = build_spec({'users(.*)$': UserResource})
    # UserResource only allows get + post
    detail = spec['paths'].get('/users/{id}', {})
    assert 'patch' not in detail
    assert 'delete' not in detail


def test_spec_list_query_parameters():
    spec = build_spec({'spaces(.*)$': SpaceResource})
    params = spec['paths']['/spaces']['get']['parameters']
    names = [p['name'] for p in params]
    assert 'page' in names
    assert 'limit' in names
    assert 'filter' in names
    assert 'fields' in names


def test_spec_custom_route_with_openapi_decorator():
    class WithCustom(BaseResource):
        model = Space
        allowed_methods = ['get']
        routes = [{'path': r'/me', 'func': 'me', 'allowed_methods': ['get']}]

        @openapi(summary='Get me', response=SpaceOut)
        async def me(self, request, match=None):
            pass

    spec = build_spec({'wc(.*)$': WithCustom})
    found = [p for p in spec['paths'] if p.endswith('/me')]
    assert found, f'custom route not in {list(spec["paths"])}'
    op = spec['paths'][found[0]]['get']
    assert op['summary'] == 'Get me'
