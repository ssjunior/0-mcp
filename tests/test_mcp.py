"""Tests for the MCP module — tool generation, JSON-RPC protocol,
bridge → dispatch end-to-end."""
import json

import pytest

from zeromcp.base import BaseResource
from zeromcp.mcp import handle_rpc, list_tools, list_tools_public, MCPResource
from zeromcp.mcp.bridge import _route_pattern_to_path, _truncate
from zeromcp.mcp.resource import mcp_view
from tests.testapp.models import Space


# ── fixtures ──────────────────────────────────────────────────────────

class _SpaceResource(BaseResource):
    model = Space
    summary = 'space'
    description = 'Workspaces.'
    list_fields = ['id', 'name', 'description', 'active']
    filter_fields = ['active']
    search_fields = ['name']
    create_fields = ['name', 'description']
    update_fields = ['name', 'description', 'active']
    authenticated = False


class _ReadOnlyResource(BaseResource):
    model = Space
    summary = 'readonly_space'
    list_fields = ['id', 'name']
    mcp_expose = ['list', 'get']
    authenticated = False


class _HiddenResource(BaseResource):
    model = Space
    summary = 'hidden'
    mcp_expose = False
    authenticated = False


ENDPOINTS = {'spaces(.*)$': _SpaceResource}
ENDPOINTS_FILTERED = {
    'spaces(.*)$': _SpaceResource,
    'readonly(.*)$': _ReadOnlyResource,
    'hidden(.*)$': _HiddenResource,
}


# ── tool generation ──────────────────────────────────────────────────

def test_list_tools_emits_all_crud_verbs():
    tools = list_tools(ENDPOINTS)
    names = [t['name'] for t in tools]
    assert 'list_space' in names
    assert 'get_space' in names
    assert 'create_space' in names
    assert 'update_space' in names
    assert 'delete_space' in names


def test_list_tools_uses_summary_for_naming():
    tools = list_tools(ENDPOINTS)
    for t in tools:
        assert 'space' in t['name']  # uses `summary='space'`


def test_list_tools_uses_description():
    tools = list_tools(ENDPOINTS)
    for t in tools:
        # Resource-level description is preserved as a prefix; the verb-
        # specific description is appended so the agent knows what the
        # tool actually does.
        assert t['description'].startswith('Workspaces.')
        assert len(t['description']) > len('Workspaces.')


def test_list_tools_includes_input_and_output_schema():
    tools = list_tools(ENDPOINTS)
    list_tool = next(t for t in tools if t['name'] == 'list_space')
    assert list_tool['inputSchema']['type'] == 'object'
    assert 'properties' in list_tool['inputSchema']
    assert 'page' in list_tool['inputSchema']['properties']
    assert 'limit' in list_tool['inputSchema']['properties']
    assert list_tool['outputSchema']['type'] == 'object'


def test_list_tools_filter_fields_are_in_input_schema():
    tools = list_tools(ENDPOINTS)
    list_tool = next(t for t in tools if t['name'] == 'list_space')
    assert 'active' in list_tool['inputSchema']['properties']


def test_get_tool_requires_id():
    tools = list_tools(ENDPOINTS)
    get_tool = next(t for t in tools if t['name'] == 'get_space')
    assert get_tool['inputSchema']['properties']['id']['type'] == 'string'
    assert 'id' in get_tool['inputSchema'].get('required', [])


def test_mcp_expose_false_hides_resource():
    tools = list_tools(ENDPOINTS_FILTERED)
    names = [t['name'] for t in tools]
    assert not any('hidden' in n for n in names)


def test_mcp_expose_list_restricts_verbs():
    tools = list_tools(ENDPOINTS_FILTERED)
    readonly = [t for t in tools if 'readonly_space' in t['name']]
    verbs = [t['mcp_internal']['kind'] for t in readonly]
    assert 'list' in verbs
    assert 'get' in verbs
    assert 'create' not in verbs
    assert 'update' not in verbs
    assert 'delete' not in verbs


def test_list_tools_public_strips_internal_meta():
    tools = list_tools_public(ENDPOINTS)
    for t in tools:
        assert 'mcp_internal' not in t


# ── JSON-RPC protocol ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initialize_returns_protocol_version():
    tools = list_tools(ENDPOINTS)
    msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}
    resp = await handle_rpc(msg, tools, {})
    assert resp['result']['protocolVersion']
    assert 'capabilities' in resp['result']
    assert resp['result']['serverInfo']['name'] == '0-mcp'


@pytest.mark.asyncio
async def test_ping():
    msg = {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}
    resp = await handle_rpc(msg, [], {})
    assert resp == {'jsonrpc': '2.0', 'id': 2, 'result': {}}


@pytest.mark.asyncio
async def test_tools_list_strips_internal():
    tools = list_tools(ENDPOINTS)
    msg = {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}
    resp = await handle_rpc(msg, tools, {})
    for t in resp['result']['tools']:
        assert 'mcp_internal' not in t


@pytest.mark.asyncio
async def test_unknown_method_returns_error():
    msg = {'jsonrpc': '2.0', 'id': 4, 'method': 'unknown/method'}
    resp = await handle_rpc(msg, [], {})
    assert resp['error']['code'] == -32601


@pytest.mark.asyncio
async def test_notification_returns_none():
    msg = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
    resp = await handle_rpc(msg, [], {})
    assert resp is None


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error_envelope():
    tools = list_tools(ENDPOINTS)
    msg = {
        'jsonrpc': '2.0', 'id': 5,
        'method': 'tools/call',
        'params': {'name': 'nonexistent_tool', 'arguments': {}},
    }
    resp = await handle_rpc(msg, tools, {})
    assert resp['result']['isError'] is True
    envelope = json.loads(resp['result']['content'][0]['text'])
    assert envelope['code'] == 'NOT_FOUND'


@pytest.mark.asyncio
async def test_invalid_request_format():
    resp = await handle_rpc('not a dict', [], {})
    assert resp['error']['code'] == -32600


@pytest.mark.asyncio
async def test_missing_method():
    resp = await handle_rpc({'jsonrpc': '2.0', 'id': 1}, [], {})
    assert resp['error']['code'] == -32600


# ── bridge helpers ───────────────────────────────────────────────────

def test_route_pattern_to_path_strips_regex():
    assert _route_pattern_to_path('spaces(.*)$') == '/spaces'
    assert _route_pattern_to_path('users(.*)$') == '/users'
    assert _route_pattern_to_path('items') == '/items'


def test_build_request_propagates_client_envelope():
    """Without envelope, RequestFactory leaves REMOTE_ADDR=127.0.0.1 and
    rate limiter exempts the caller. The bridge must inject the outer
    request's client identity."""
    from zeromcp.mcp.bridge import _build_request

    envelope = {
        'remote_addr': '203.0.113.7',
        'forwarded_for': '203.0.113.7, 10.0.0.1',
        'real_ip': '203.0.113.7',
        'user_agent': 'mcp-agent/1.0',
    }
    request = _build_request('GET', '/spaces', envelope=envelope)

    assert request.META['REMOTE_ADDR'] == '203.0.113.7'
    assert request.META['HTTP_X_FORWARDED_FOR'] == '203.0.113.7, 10.0.0.1'
    assert request.META['HTTP_X_REAL_IP'] == '203.0.113.7'
    assert request.META['HTTP_USER_AGENT'] == 'mcp-agent/1.0'


def test_build_request_without_envelope_keeps_default():
    """Backwards-compat: envelope is optional (e.g. stdio transport has
    no real IP). Without it, the synthetic request looks loopback."""
    from zeromcp.mcp.bridge import _build_request

    request = _build_request('GET', '/spaces')
    # RequestFactory default
    assert request.META.get('REMOTE_ADDR') == '127.0.0.1'


def test_truncate_caps_list_length():
    payload = {'objects': list(range(200)), 'meta': {}}
    out = _truncate(payload, max_list=10)
    assert len(out['objects']) == 10
    assert out['meta']['truncated'] is True
    assert out['meta']['returned'] == 10


def test_truncate_long_string():
    payload = {'note': 'x' * 5000}
    out = _truncate(payload, max_text=100)
    assert len(out['note']) <= 200
    assert 'truncated' in out['note']


def test_truncate_passes_through_small_payloads():
    payload = {'objects': [{'id': 1}], 'meta': {}}
    out = _truncate(payload, max_list=50)
    assert out == payload


# ── MCPResource integration ──────────────────────────────────────────

def test_mcp_view_returns_a_view():
    view = mcp_view(ENDPOINTS)
    assert callable(view)


def test_mcp_resource_subclass_carries_endpoints():
    class MyMCP(MCPResource):
        endpoints = ENDPOINTS

    inst = MyMCP()
    assert any(t['name'] == 'list_space' for t in inst.tools)


def test_mcp_resource_does_not_expose_itself():
    assert MCPResource.mcp_expose is False
