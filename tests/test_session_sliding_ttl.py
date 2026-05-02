"""Sliding TTL on cookie sessions and the routes auth gate.

The api-key path has its own coverage in ``test_api_key_resolver``; this
file covers the cookie path through ``AuthMiddleware`` and the
``_has_valid_session`` gate used by ``/docs`` / ``/openapi.json``.
"""
import json

import pytest

from zeromcp import middleware as mw_mod
from zeromcp import routes as routes_mod
from zeromcp.middleware import AuthMiddleware
from zeromcp.redis_config import KEY_PREFIX
from zeromcp.routes import _has_valid_session


SID = 'a' * 32  # validate_session_key requires alnum, length 1-128


def _request_with_cookie(sid=SID, **headers):
    """Lightweight Django-ish request stub. Middleware reads
    ``request.COOKIES`` and ``request.headers``; nothing else."""
    class Req:
        pass
    req = Req()
    req.COOKIES = {mw_mod.COOKIE_ID: sid} if sid else {}
    req.headers = headers
    return req


# ── middleware sliding TTL ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_middleware_renews_cookie_session_ttl(
    monkeypatch, fake_redis,
):
    """Each authenticated request should reset the TTL on the session
    key — i.e. an active user is not logged out by the original window."""
    monkeypatch.setattr(mw_mod, 'SESSION_TTL', 100)
    key = f'{KEY_PREFIX}sessions:{SID}'
    await fake_redis.set(
        key, json.dumps({'user': {'id': 1}}), ex=10,
    )

    async def get_response(request):
        return 'ok'

    middleware = AuthMiddleware(get_response)
    request = _request_with_cookie()
    await middleware(request)

    ttl = await fake_redis.ttl(key)
    assert 50 < ttl <= 100   # reset to ~SESSION_TTL


@pytest.mark.asyncio
async def test_middleware_misses_dont_create_keys(monkeypatch, fake_redis):
    """A cookie that points to a non-existent session must NOT
    materialize a Redis key (no auth bypass via cookie forging)."""
    monkeypatch.setattr(mw_mod, 'SESSION_TTL', 100)
    key = f'{KEY_PREFIX}sessions:{SID}'

    async def get_response(request):
        return 'ok'

    middleware = AuthMiddleware(get_response)
    request = _request_with_cookie()
    await middleware(request)

    assert await fake_redis.get(key) is None


@pytest.mark.asyncio
async def test_middleware_anonymous_shape_when_no_cookie(
    monkeypatch, fake_redis,
):
    monkeypatch.setattr(mw_mod, 'SESSION_TTL', 100)

    async def get_response(request):
        return 'ok'

    middleware = AuthMiddleware(get_response)
    request = _request_with_cookie(sid=None)
    await middleware(request)

    assert request.user is None
    assert request.authenticated is False
    assert request.account is None
    assert request.account_id is None


@pytest.mark.asyncio
async def test_middleware_clears_tenant_for_session_without_account(
    monkeypatch, fake_redis,
):
    """Cookie session without account in a previously-tenant-bound
    request must reset the tenant context."""
    from zeromcp.tenant import tenant as tenant_mod
    monkeypatch.setattr(mw_mod, 'SESSION_TTL', 100)
    tenant_mod.db_state.set('tenant_old')
    key = f'{KEY_PREFIX}sessions:{SID}'
    await fake_redis.set(
        key, json.dumps({'user': {'id': 1}}),
    )

    async def get_response(request):
        return 'ok'

    middleware = AuthMiddleware(get_response)
    request = _request_with_cookie()
    await middleware(request)

    assert tenant_mod.db_state.get() == 'default'


# ── routes gate sliding TTL ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_has_valid_session_renews_cookie_ttl(
    monkeypatch, fake_redis,
):
    """``/docs`` and friends must also slide the cookie TTL — otherwise
    a user sitting on docs gets logged out mid-browse."""
    monkeypatch.setattr(routes_mod, 'SESSION_TTL', 100)
    key = f'{KEY_PREFIX}sessions:{SID}'
    await fake_redis.set(
        key, json.dumps({'user': {'id': 1}}), ex=10,
    )

    request = _request_with_cookie()
    assert await _has_valid_session(request) is True

    ttl = await fake_redis.ttl(key)
    assert 50 < ttl <= 100


@pytest.mark.asyncio
async def test_has_valid_session_returns_false_on_miss(
    monkeypatch, fake_redis,
):
    monkeypatch.setattr(routes_mod, 'SESSION_TTL', 100)
    request = _request_with_cookie()
    assert await _has_valid_session(request) is False
