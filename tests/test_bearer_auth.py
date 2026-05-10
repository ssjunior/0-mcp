"""Bearer token authentication — opt-in, parallel to X-Api-Key.

Tests cover:
- Parser tri-state (absent / invalid / token)
- Resolver activation through ``BEARER_RESOLVER`` setting
- Precedence over X-Api-Key/cookie
- ``REQUIRE_VALID_BEARER`` strict mode (only on authed routes)
- Resolver exception handling (warning, no token leak)
- Discovery endpoint (RFC 9728) + ImproperlyConfigured boot validation
- OpenAPI bearerAuth coexists with apiKeyAuth
"""
import json
import logging
import types

import pytest
from django.core.exceptions import ImproperlyConfigured

from zeromcp import bearer
from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


# ── parser ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('header,expected', [
    (None, ('absent', None)),
    ('', ('absent', None)),
    ('   ', ('absent', None)),
    ('Basic xyz', ('absent', None)),
    ('Bearer', ('invalid', None)),
    ('Bearer ', ('invalid', None)),
    ('Bearer a b', ('invalid', None)),
    ('Bearer  abc', ('invalid', None)),  # double space → leading space on token
    ('Bearer abc', ('token', 'abc')),
    ('bearer abc', ('token', 'abc')),     # case-insensitive scheme
    ('BEARER abc', ('token', 'abc')),
    ('  Bearer abc  ', ('token', 'abc')),  # outer whitespace strip
])
def test_parse_bearer(header, expected):
    assert bearer.parse_bearer(header) == expected


# ── resolver wiring ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_resolver_caches():
    """Reset both resolver caches around every test. Using an autouse
    fixture instead of inline cleanup so a failing assertion can't leak
    state into subsequent tests."""
    from zeromcp.tenant import tenant
    bearer._resolver_cache = None
    tenant._resolver_cache = None
    yield
    bearer._resolver_cache = None
    tenant._resolver_cache = None


VALID_TOKEN = 'valid-bearer-token'
SAMPLE_SESSION = {
    'user': {'id': 7, 'email': 'b@c.com'},
    'account': {'id': 1, 'name': 'Test'},
}


async def good_resolver(token):
    if token == VALID_TOKEN:
        return SAMPLE_SESSION
    return None


def good_resolver_sync(token):
    """Sync resolver — no coroutine returned."""
    if token == VALID_TOKEN:
        return SAMPLE_SESSION
    return None


def wrapped_resolver(token):
    """Sync callable that *returns* a coroutine — ``iscoroutinefunction``
    fails on this (the function itself isn't a coroutine function), but
    ``isawaitable`` on the result detects it."""
    return good_resolver(token)


def crashing_resolver(token):
    raise RuntimeError('database is down')


async def _noop_aset_tenant(account_id):
    return 'default'


def _set_resolver(monkeypatch, resolver, *, require_valid=False):
    monkeypatch.setattr(
        bearer, 'get_bearer_resolver', lambda: resolver,
    )
    monkeypatch.setattr(
        'zeromcp.base.get_require_valid_bearer', lambda: require_valid,
    )
    # Avoid hitting the DB during dispatch tests — multi-tenant env is
    # configured but we don't need real tenant routing.
    monkeypatch.setattr('zeromcp.base.aset_tenant', _noop_aset_tenant)
    monkeypatch.setattr(
        'zeromcp.tenant.tenant.aset_tenant', _noop_aset_tenant,
    )
    bearer._resolver_cache = None


# ── direct resolve_bearer_session ─────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_no_resolver_configured(monkeypatch):
    _set_resolver(monkeypatch, None)
    code, session = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.NO_RESOLVER
    assert session is None


@pytest.mark.asyncio
async def test_resolve_valid_token_async(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    code, session = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.OK
    assert session == SAMPLE_SESSION


@pytest.mark.asyncio
async def test_resolve_valid_token_sync(monkeypatch):
    _set_resolver(monkeypatch, good_resolver_sync)
    code, session = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.OK
    assert session == SAMPLE_SESSION


@pytest.mark.asyncio
async def test_resolve_sync_wrapper_returning_coroutine(monkeypatch):
    """Wrapper/decorator pattern: the callable itself is sync, but its
    return value is awaitable. ``isawaitable`` on the result handles
    this; ``iscoroutinefunction`` on the callable would not."""
    _set_resolver(monkeypatch, wrapped_resolver)
    code, session = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.OK


@pytest.mark.asyncio
async def test_resolve_invalid_token(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    code, session = await bearer.resolve_bearer_session('Bearer wrong')
    assert code == bearer.FAIL_INVALID
    assert session is None


@pytest.mark.asyncio
async def test_resolve_malformed_header(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    code, session = await bearer.resolve_bearer_session('Bearer  malformed')
    assert code == bearer.FAIL_INVALID


@pytest.mark.asyncio
async def test_resolve_resolver_exception(monkeypatch, caplog):
    _set_resolver(monkeypatch, crashing_resolver)
    with caplog.at_level(logging.WARNING):
        code, session = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.FAIL_EXCEPTION
    # Token never appears raw in logs.
    log_text = caplog.text
    assert VALID_TOKEN not in log_text
    # Hash prefix (12 chars) appears.
    from hashlib import sha256
    h = sha256(VALID_TOKEN.encode()).hexdigest()[:12]
    assert h in log_text


def leaky_resolver(token):
    """Worst case: resolver embeds the token in its exception message.
    The framework must not leak it via traceback or message."""
    raise RuntimeError(f'database barfed on token {token}')


@pytest.mark.asyncio
async def test_resolve_token_never_leaks_via_exception_message(
    monkeypatch, caplog,
):
    """If the resolver puts the token in its exception message, the
    framework must still keep it out of logs. ``exc_info=True`` would
    leak it via the traceback — guard against that regression."""
    _set_resolver(monkeypatch, leaky_resolver)
    with caplog.at_level(logging.WARNING):
        code, _ = await bearer.resolve_bearer_session(f'Bearer {VALID_TOKEN}')
    assert code == bearer.FAIL_EXCEPTION
    # Neither the raw token nor the leaky message must appear.
    assert VALID_TOKEN not in caplog.text
    assert 'database barfed on token' not in caplog.text
    # But the exception type name must be present for diagnostics.
    assert 'RuntimeError' in caplog.text


@pytest.mark.asyncio
async def test_resolve_no_resolver_returns_no_resolver_code(monkeypatch):
    """The fix for the strict+no-resolver retrocompat trap: when
    ``BEARER_RESOLVER`` is unset, the resolver must return
    ``NO_RESOLVER`` distinctly from ``NOT_PRESENT``."""
    _set_resolver(monkeypatch, None)
    code, _ = await bearer.resolve_bearer_session('Bearer abc')
    assert code == bearer.NO_RESOLVER


@pytest.mark.asyncio
async def test_resolve_basic_scheme_returns_absent(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    code, session = await bearer.resolve_bearer_session('Basic xyz')
    assert code == bearer.NOT_PRESENT


# ── BaseResource integration ──────────────────────────────────────────

class _FakeGet:
    def get(self, key, default=None):
        return default

    def urlencode(self):
        return ''

    def __iter__(self):
        return iter([])

    def __contains__(self, key):
        return False


def make_request(headers=None, *, cookies=None, path='/api/test'):
    req = types.SimpleNamespace()
    req.method = 'GET'
    req.path = path
    req.path_info = path
    req.body = b''
    req.content_type = 'application/json'
    req.headers = headers or {}
    req.COOKIES = cookies or {}
    req.GET = _FakeGet()
    req.META = {'REMOTE_ADDR': '127.0.0.1'}
    return req


class AuthedResource(BaseResource):
    model = Space
    authenticated = True

    async def get(self, request):
        return {'success': True, 'user_id': self.user.get('id')}


class PublicResource(BaseResource):
    model = Space
    authenticated = False

    async def get(self, request):
        return {'success': True}


@pytest.mark.asyncio
async def test_dispatch_bearer_authenticates(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    req = make_request({'Authorization': f'Bearer {VALID_TOKEN}'})
    res = AuthedResource()
    response = await res.dispatch(req)
    body = json.loads(response.content)
    assert response.status_code == 200
    assert body['user_id'] == 7
    # Request shape populated through apply_session_to_request.
    assert getattr(req, 'authenticated', False) is True
    assert req.user == SAMPLE_SESSION['user']


@pytest.mark.asyncio
async def test_dispatch_bearer_wins_over_cookie_and_apikey(monkeypatch):
    """Bearer + cookie + X-Api-Key all present → Bearer wins."""
    _set_resolver(monkeypatch, good_resolver)
    req = make_request(
        headers={
            'Authorization': f'Bearer {VALID_TOKEN}',
            'X-Api-Key': 'should-be-ignored',
        },
        cookies={'sessionid': 'should-be-ignored'},
    )
    res = AuthedResource()
    response = await res.dispatch(req)
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body['user_id'] == 7


@pytest.mark.asyncio
async def test_dispatch_invalid_bearer_loose_falls_back_to_anon(monkeypatch):
    """REQUIRE_VALID_BEARER=False, invalid token → 401 from authenticated
    flag (not from Bearer strict). Confirms fallthrough, not strict block."""
    _set_resolver(monkeypatch, good_resolver, require_valid=False)
    req = make_request({'Authorization': 'Bearer wrong'})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    # 401 from "Not authorized" because no other auth either, NOT
    # from strict bearer mode.
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_strict_invalid_bearer_returns_401(monkeypatch):
    _set_resolver(monkeypatch, good_resolver, require_valid=True)
    req = make_request({'Authorization': 'Bearer wrong'})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_strict_no_header_on_authed_route(monkeypatch):
    _set_resolver(monkeypatch, good_resolver, require_valid=True)
    req = make_request({})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_strict_does_not_block_public_routes(monkeypatch):
    """Critical: strict mode must not promote public routes to auth-required."""
    _set_resolver(monkeypatch, good_resolver, require_valid=True)
    req = make_request({})
    res = PublicResource()
    response = await res.dispatch(req)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_dispatch_resolver_exception_returns_401_not_500(monkeypatch, caplog):
    _set_resolver(monkeypatch, crashing_resolver)
    req = make_request({'Authorization': f'Bearer {VALID_TOKEN}'})
    res = AuthedResource()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as exc:
            await res.dispatch(req)
    assert exc.value.args[0] == 401
    # Token never raw in logs.
    assert VALID_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_dispatch_basic_scheme_loose_falls_through(monkeypatch):
    """Authorization: Basic + not strict → falls back; no other creds → 401."""
    _set_resolver(monkeypatch, good_resolver, require_valid=False)
    req = make_request({'Authorization': 'Basic xyz'})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_basic_scheme_strict_returns_401(monkeypatch):
    """Authorization: Basic + strict → 401 (no fallback to other auth)."""
    _set_resolver(monkeypatch, good_resolver, require_valid=True)
    req = make_request({'Authorization': 'Basic xyz'})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_no_resolver_behaves_like_1_3(monkeypatch):
    """``BEARER_RESOLVER`` unset → comportamento idêntico a 1.3.x.
    Sem outras credenciais, 401 vem do gate ``self.authenticated``."""
    _set_resolver(monkeypatch, None)
    req = make_request({'Authorization': f'Bearer {VALID_TOKEN}'})
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401


@pytest.mark.asyncio
async def test_dispatch_no_resolver_with_strict_does_not_block_xapikey(
    monkeypatch,
):
    """Critical retrocompat case: project sets ``REQUIRE_VALID_BEARER=True``
    but never configured ``BEARER_RESOLVER``. Strict mode must NOT kick
    in — otherwise a 1.3.x project that flips the flag breaks every
    X-Api-Key/cookie request."""
    _set_resolver(monkeypatch, None, require_valid=True)

    # Stub the api-key resolver so X-Api-Key actually resolves.
    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    req = make_request({'X-Api-Key': 'good-key'})
    res = AuthedResource()
    response = await res.dispatch(req)
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body['user_id'] == 7



@pytest.mark.asyncio
async def test_dispatch_invalid_bearer_loose_falls_back_to_xapikey(
    monkeypatch,
):
    """Invalid Bearer + valid X-Api-Key + strict=False → X-Api-Key wins.
    Proves real fallback, not just an end-state 401."""
    _set_resolver(monkeypatch, good_resolver, require_valid=False)

    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    req = make_request({
        'Authorization': 'Bearer wrong',
        'X-Api-Key': 'good-key',
    })
    res = AuthedResource()
    response = await res.dispatch(req)
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body['user_id'] == 7



@pytest.mark.asyncio
async def test_dispatch_basic_loose_falls_back_to_xapikey(monkeypatch):
    """Authorization: Basic + valid X-Api-Key + strict=False → X-Api-Key
    flow runs and authenticates. The Bearer parser must classify Basic
    as ABSENT, otherwise the fallback would never be reached."""
    _set_resolver(monkeypatch, good_resolver, require_valid=False)

    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    req = make_request({
        'Authorization': 'Basic xyz',
        'X-Api-Key': 'good-key',
    })
    res = AuthedResource()
    response = await res.dispatch(req)
    assert response.status_code == 200



@pytest.mark.asyncio
async def test_dispatch_strict_blocks_xapikey_when_resolver_present(
    monkeypatch,
):
    """The other half of the strict contract: with resolver configured
    AND strict=True, even a perfectly valid X-Api-Key must NOT be
    accepted — Bearer is the only path."""
    _set_resolver(monkeypatch, good_resolver, require_valid=True)

    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    req = make_request({'X-Api-Key': 'good-key'})  # no Bearer at all
    res = AuthedResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(req)
    assert exc.value.args[0] == 401



# ── Discovery / ImproperlyConfigured ──────────────────────────────────

def test_discovery_metadata_emits_rfc9728_shape(monkeypatch):
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_as_url',
        lambda: 'https://auth.example.com',
    )
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_resource_url',
        lambda: 'https://api.example.com',
    )
    metadata = bearer.discovery_metadata()
    assert metadata == {
        'resource': 'https://api.example.com',
        'authorization_servers': ['https://auth.example.com'],
        'bearer_methods_supported': ['header'],
    }


def test_discovery_raises_when_resource_url_missing(monkeypatch):
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_as_url',
        lambda: 'https://auth.example.com',
    )
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_resource_url', lambda: None,
    )
    with pytest.raises(ImproperlyConfigured):
        bearer.discovery_metadata()


def test_get_routes_raises_when_misconfigured(monkeypatch):
    """Half-configured discovery must fail at boot, not at request."""
    monkeypatch.setattr(
        'zeromcp.routes.get_oauth_as_url',
        lambda: 'https://auth.example.com',
    )
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_as_url',
        lambda: 'https://auth.example.com',
    )
    monkeypatch.setattr(
        'zeromcp.bearer.get_oauth_resource_url', lambda: None,
    )
    from zeromcp.routes import get_routes
    with pytest.raises(ImproperlyConfigured):
        get_routes({})


# ── OpenAPI: bearerAuth coexists with existing schemes ────────────────

def test_openapi_includes_bearer_auth_alongside_api_key():
    from zeromcp.openapi import build_spec
    spec = build_spec({})
    schemes = spec['components']['securitySchemes']
    # X-Api-Key must remain intact.
    assert schemes['apiKeyAuth'] == {
        'type': 'apiKey', 'in': 'header', 'name': 'X-Api-Key',
    }
    # Cookie too.
    assert schemes['cookieAuth']['type'] == 'apiKey'
    # New: bearer.
    assert schemes['bearerAuth'] == {'type': 'http', 'scheme': 'bearer'}
    # Top-level security lists all three.
    assert {'apiKeyAuth': []} in spec['security']
    assert {'cookieAuth': []} in spec['security']
    assert {'bearerAuth': []} in spec['security']


# ── _has_valid_session (docs/openapi gate) ────────────────────────────

@pytest.mark.asyncio
async def test_has_valid_session_accepts_bearer(monkeypatch):
    _set_resolver(monkeypatch, good_resolver)
    from zeromcp.routes import _has_valid_session
    req = make_request({'Authorization': f'Bearer {VALID_TOKEN}'})
    assert await _has_valid_session(req) is True


@pytest.mark.asyncio
async def test_has_valid_session_strict_blocks_xapikey(monkeypatch):
    _set_resolver(monkeypatch, good_resolver, require_valid=True)
    monkeypatch.setattr(
        'zeromcp.routes.get_require_valid_bearer', lambda: True,
    )

    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    from zeromcp.routes import _has_valid_session
    req = make_request({'X-Api-Key': 'good-key'})
    assert await _has_valid_session(req) is False



@pytest.mark.asyncio
async def test_has_valid_session_no_resolver_strict_does_not_block(
    monkeypatch,
):
    """Same retrocompat trap as in dispatch: strict + no resolver must
    fall through to the existing X-Api-Key/cookie gate."""
    _set_resolver(monkeypatch, None, require_valid=True)
    monkeypatch.setattr(
        'zeromcp.routes.get_require_valid_bearer', lambda: True,
    )

    from zeromcp.tenant import tenant
    async def _api_key_resolver(key):
        return SAMPLE_SESSION if key == 'good-key' else None
    monkeypatch.setattr(
        tenant, 'MCP_API_KEY_RESOLVER', _api_key_resolver,
    )

    from zeromcp.routes import _has_valid_session
    req = make_request({'X-Api-Key': 'good-key'})
    assert await _has_valid_session(req) is True



@pytest.mark.asyncio
async def test_has_valid_session_resolver_exception_blocks(monkeypatch):
    _set_resolver(monkeypatch, crashing_resolver)
    from zeromcp.routes import _has_valid_session
    req = make_request({'Authorization': f'Bearer {VALID_TOKEN}'})
    assert await _has_valid_session(req) is False


# ── Compat alias for promoted validator ───────────────────────────────

def test_validate_session_shape_alias_still_importable():
    from zeromcp.tenant.tenant import (
        _validate_session_shape, validate_session_shape,
    )
    assert _validate_session_shape is validate_session_shape
