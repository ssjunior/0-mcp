"""Strict shape checks for resolver output and cached api-key entries.

Covers:
- single-tenant rejects ``account``/``account_id`` (contract bug);
- multi-tenant requires ``account`` dict with ``id``, plus
  ``account_id`` consistency when both fields are present;
- cache hits are re-validated on read (poisoned entries get deleted).
"""
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


def _api_session_key(api_key):
    return f'{KEY_PREFIX}api_session:' + (
        sha256(api_key.encode('utf-8')).hexdigest()[:16]
    )


# ── single-tenant ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_tenant_accepts_session_without_account(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', None)

    async def resolver(api_key):
        return {'user': {'id': 1, 'email': 'a@b.com'}}

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    out = await tenant.get_api_session('k')
    assert out == {'user': {'id': 1, 'email': 'a@b.com'}}


@pytest.mark.asyncio
async def test_single_tenant_rejects_account_in_session(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', None)

    async def resolver(api_key):
        return {
            'user': {'id': 1},
            'account': {'id': 9},  # contract bug — single-tenant
        }

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


@pytest.mark.asyncio
async def test_single_tenant_rejects_account_id_in_session(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', None)

    async def resolver(api_key):
        return {'user': {'id': 1}, 'account_id': 9}

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


# ── multi-tenant ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_tenant_requires_account_dict(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', object())

    async def resolver(api_key):
        return {'user': {'id': 1}}  # missing account

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


@pytest.mark.asyncio
async def test_multi_tenant_requires_account_id_field(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', object())

    async def resolver(api_key):
        return {'user': {'id': 1}, 'account': {'name': 'Acme'}}

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


@pytest.mark.asyncio
async def test_multi_tenant_rejects_divergent_account_id(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', object())

    async def resolver(api_key):
        return {
            'user': {'id': 1},
            'account': {'id': 7},
            'account_id': 9,  # divergent
        }

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


@pytest.mark.asyncio
async def test_multi_tenant_accepts_consistent_account_id(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', object())

    async def resolver(api_key):
        return {
            'user': {'id': 1},
            'account': {'id': 7},
            'account_id': 7,
        }

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    out = await tenant.get_api_session('k')
    assert out['account']['id'] == 7


# ── shape — non-dict pieces ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_must_be_dict(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', None)

    async def resolver(api_key):
        return {'user': 'not-a-dict'}

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


@pytest.mark.asyncio
async def test_session_must_be_dict(monkeypatch):
    monkeypatch.setattr(tenant, 'account_model', None)

    async def resolver(api_key):
        return ['not', 'a', 'dict']

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    assert await tenant.get_api_session('k') is None


# ── cache poisoning — re-validation on read ───────────────────────────


@pytest.mark.asyncio
async def test_cached_entry_revalidated_and_dropped(monkeypatch, fake_redis):
    """Older code (or a since-tightened contract) primed Redis with a
    session that no longer matches the rules. Read path must reject it,
    delete the key, and fall back to the resolver."""
    monkeypatch.setattr(tenant, 'account_model', None)
    api_key = 'opaque-x'
    cache_key = _api_session_key(api_key)
    # Poison: account in single-tenant mode — invalid under current rules.
    poisoned = {'user': {'id': 1}, 'account': {'id': 9}}
    await fake_redis.set(cache_key, json.dumps(poisoned))

    async def resolver(api_key):
        return {'user': {'id': 99, 'email': 'fresh@x.com'}}

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    out = await tenant.get_api_session(api_key)
    # Resolver-fresh value, not the poisoned one
    assert out['user']['id'] == 99
    # Cache now holds the validated value
    cached = await fake_redis.get(cache_key)
    assert json.loads(cached)['user']['id'] == 99


@pytest.mark.asyncio
async def test_cached_entry_revalidated_falls_through_when_resolver_fails(
    monkeypatch, fake_redis,
):
    """Poisoned cache + resolver returning None → final result is None,
    poisoned entry is gone."""
    monkeypatch.setattr(tenant, 'account_model', None)
    api_key = 'opaque-y'
    cache_key = _api_session_key(api_key)
    await fake_redis.set(
        cache_key, json.dumps({'user': 'not-a-dict'}),
    )

    async def resolver(api_key):
        return None

    monkeypatch.setattr(tenant, 'MCP_API_KEY_RESOLVER', resolver)
    out = await tenant.get_api_session(api_key)
    assert out is None
    assert await fake_redis.get(cache_key) is None
