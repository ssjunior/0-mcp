"""Pluggable API key resolver — backwards compatible, custom override."""
import json
from hashlib import sha256

import pytest

from zeromcp.tenant import tenant
from zeromcp.redis_config import KEY_PREFIX


@pytest.fixture(autouse=True)
def reset_resolver_cache():
    tenant._resolver_cache = None
    yield
    tenant._resolver_cache = None


def _make_legacy_key(account_id='42', uuid='abc', salt='xyz'):
    payload = f'{account_id}{uuid}{salt}'.encode()
    h = sha256(payload).hexdigest()[:tenant.HASH_LENGTH]
    return f'{account_id}.{uuid}.{salt}.{h}'


# ── default resolver — backwards-compat ────────────────────────────────

@pytest.mark.asyncio
async def test_default_resolver_rejects_malformed_key():
    out = await tenant._default_resolve_api_key('not-a-key')
    assert out is None


@pytest.mark.asyncio
async def test_default_resolver_rejects_bad_hash():
    bad = '42.abc.xyz.WRONGHASH'
    out = await tenant._default_resolve_api_key(bad)
    assert out is None


@pytest.mark.asyncio
async def test_load_resolver_defaults_when_unset(monkeypatch):
    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', None)
    tenant._resolver_cache = None
    resolver = tenant._load_resolver()
    assert resolver is tenant._default_resolve_api_key


# ── custom resolver via dotted path ────────────────────────────────────

async def opaque_token_resolver(api_key):
    """Example custom resolver — opaque random tokens, no embedded tenant."""
    if api_key == 'opaque-valid-token':
        return {
            'user': {'id': 99, 'email': 'a@b.com'},
            'account': {'id': 5, 'name': 'Acme'},
        }
    return None


@pytest.mark.asyncio
async def test_custom_resolver_via_callable(monkeypatch):
    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', opaque_token_resolver)
    tenant._resolver_cache = None
    resolver = tenant._load_resolver()
    out = await resolver('opaque-valid-token')
    assert out['user']['id'] == 99


@pytest.mark.asyncio
async def test_custom_resolver_via_dotted_path(monkeypatch):
    dotted = f'{__name__}.opaque_token_resolver'
    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', dotted)
    tenant._resolver_cache = None
    resolver = tenant._load_resolver()
    out = await resolver('opaque-valid-token')
    assert out['user']['id'] == 99


# ── get_api_session caches the result ──────────────────────────────────

@pytest.mark.asyncio
async def test_get_api_session_caches_in_redis(monkeypatch, fake_redis):
    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', opaque_token_resolver)
    tenant._resolver_cache = None

    out1 = await tenant.get_api_session('opaque-valid-token')
    assert out1['user']['id'] == 99

    key_hash = sha256('opaque-valid-token'.encode('utf-8')).hexdigest()[:16]
    cached = await fake_redis.get(f'{KEY_PREFIX}api_session:{key_hash}')
    assert cached
    assert json.loads(cached)['user']['id'] == 99


@pytest.mark.asyncio
async def test_get_api_session_returns_none_when_resolver_fails(monkeypatch, fake_redis):
    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', opaque_token_resolver)
    tenant._resolver_cache = None

    out = await tenant.get_api_session('unknown-token')
    assert out is None
    key_hash = sha256('unknown-token'.encode('utf-8')).hexdigest()[:16]
    cached = await fake_redis.get(f'{KEY_PREFIX}api_session:{key_hash}')
    assert cached is None
