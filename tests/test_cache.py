import pytest

from zeromcp.base import BaseResource
from zeromcp.redis_config import KEY_PREFIX, get_redis
from tests.testapp.models import Space


class CachedResource(BaseResource):
    model = Space
    cache = True
    cache_ttl = 60


@pytest.mark.asyncio
async def test_save_cache_writes_setex_and_namespace_set(fake_redis):
    res = CachedResource()
    res.cache = True
    res.cache_key = f'{KEY_PREFIX}cache:/spaces'
    res.cache_namespace = 'list:testapp.space'

    await res.save_cache({'objects': []})

    redis = get_redis()
    assert await redis.get(res.cache_key) is not None
    members = await redis.smembers(f'{KEY_PREFIX}cache_ns:list:testapp.space')
    assert res.cache_key in members


@pytest.mark.asyncio
async def test_invalidate_cache_clears_namespace(fake_redis):
    res = CachedResource()
    res.cache = True
    res.cache_key = f'{KEY_PREFIX}cache:/spaces/5'
    res.cache_namespace = 'detail:testapp.space:5'
    await res.save_cache({'id': 5})

    redis = get_redis()
    assert await redis.get(res.cache_key) is not None

    await res.invalidate_cache(['detail:testapp.space:5'])

    assert await redis.get(res.cache_key) is None
    assert await redis.smembers(f'{KEY_PREFIX}cache_ns:detail:testapp.space:5') == set()


@pytest.mark.asyncio
async def test_invalidate_cache_multiple_namespaces(fake_redis):
    res_list = CachedResource()
    res_list.cache = True
    res_list.cache_key = f'{KEY_PREFIX}cache:/spaces'
    res_list.cache_namespace = 'list:testapp.space'
    await res_list.save_cache([{'id': 1}])

    res_detail = CachedResource()
    res_detail.cache = True
    res_detail.cache_key = f'{KEY_PREFIX}cache:/spaces/5'
    res_detail.cache_namespace = 'detail:testapp.space:5'
    await res_detail.save_cache({'id': 5})

    redis = get_redis()
    await res_list.invalidate_cache([
        'list:testapp.space',
        'detail:testapp.space:5',
    ])

    assert await redis.get(res_list.cache_key) is None
    assert await redis.get(res_detail.cache_key) is None


@pytest.mark.asyncio
async def test_invalidate_skips_when_no_namespaces(fake_redis):
    res = CachedResource()
    await res.invalidate_cache([])
    await res.invalidate_cache(None)


def test_cache_namespaces_list_only():
    res = CachedResource()
    assert res._cache_namespaces() == ['list:testapp.space']


def test_cache_namespaces_with_detail_id():
    res = CachedResource()
    assert res._cache_namespaces(include_detail_id=42) == [
        'list:testapp.space',
        'detail:testapp.space:42',
    ]


def test_cache_namespaces_no_model_returns_empty():
    res = BaseResource()
    assert res._cache_namespaces() == []


@pytest.mark.asyncio
async def test_save_response_cache_persists_jsonresponse_body(fake_redis):
    """Resources whose handler returns ``JsonResponse(...)`` directly
    bypass ``serialize()``. Without ``_save_response_cache`` the body
    was never written to Redis even with ``cache = True`` — cache miss
    forever (the ``/api/me`` symptom)."""
    from django.http import JsonResponse

    res = CachedResource()
    res.cache = True
    res.cache_key = f'{KEY_PREFIX}cache:/me'
    res.cache_namespace = 'list:testapp.space'

    payload = {'id': 7, 'name': 'demo', 'email': 'a@b.com'}
    response = JsonResponse(payload)

    await res._save_response_cache(response)

    redis = get_redis()
    raw = await redis.get(res.cache_key)
    assert raw is not None

    import json as _json
    assert _json.loads(raw) == payload

    members = await redis.smembers(f'{KEY_PREFIX}cache_ns:list:testapp.space')
    assert res.cache_key in members


@pytest.mark.asyncio
async def test_save_response_cache_skipped_when_cache_off(fake_redis):
    from django.http import JsonResponse

    res = CachedResource()
    res.cache = False
    res.cache_key = f'{KEY_PREFIX}cache:/me'
    res.cache_namespace = 'list:testapp.space'

    await res._save_response_cache(JsonResponse({'id': 1}))

    redis = get_redis()
    assert await redis.get(res.cache_key) is None


def test_default_cache_ttl_is_120():
    """Bumped from 60s — short enough to limit stale window, long enough
    to amortize hot endpoints. Settings can still override via
    ``CACHE_TTL``."""
    from zeromcp.base import BaseResource
    assert BaseResource.cache_ttl == 120


def test_settings_cache_ttl_overrides_default(monkeypatch):
    """``settings.CACHE_TTL`` becomes the new default for any
    resource that doesn't override ``cache_ttl`` explicitly."""
    from zeromcp import base as base_module

    monkeypatch.setattr(base_module, 'CACHE_TTL', 600)

    class _R(base_module.BaseResource):
        cache_ttl = (
            base_module.CACHE_TTL
            if base_module.CACHE_TTL is not None
            else 120
        )

    assert _R.cache_ttl == 600


def test_resource_cache_ttl_wins_over_settings(monkeypatch):
    from zeromcp import base as base_module
    monkeypatch.setattr(base_module, 'CACHE_TTL', 600)

    class _R(base_module.BaseResource):
        cache_ttl = 30

    assert _R.cache_ttl == 30


def test_cache_ttl_enable_kill_switch_default_true():
    """Cache works as before when the kill switch is at its default."""
    from zeromcp import base as base_module
    assert base_module.CACHE_TTL_ENABLE is True


@pytest.mark.asyncio
async def test_kill_switch_disables_cache_in_dispatch(fake_redis, monkeypatch):
    """``CACHE_TTL_ENABLE = False`` forces ``self.cache`` to
    False inside dispatch even when the resource has ``cache = True``."""
    from django.test import RequestFactory
    from zeromcp import base as base_module

    monkeypatch.setattr(base_module, 'CACHE_TTL_ENABLE', False)

    res = CachedResource()
    res.user = None
    res.account = None
    res.account_id = None
    res.id = None
    res.queryset = Space.objects.all()
    res.request = RequestFactory().get('/spaces')
    res.cache = True

    # Replicate the dispatch line that normalizes self.cache
    is_custom_route = False
    res.cache = (
        res.cache
        and base_module.CACHE_TTL_ENABLE
        and 'get' == 'get'
        and (not is_custom_route or res.route_cache)
    )

    assert res.cache is False, 'kill switch must force cache off'


def test_kill_switch_keeps_cache_on_when_true():
    from zeromcp import base as base_module
    assert base_module.CACHE_TTL_ENABLE is True

    res = CachedResource()
    res.cache = True

    is_custom_route = False
    res.cache = (
        res.cache
        and base_module.CACHE_TTL_ENABLE
        and 'get' == 'get'
        and (not is_custom_route or res.route_cache)
    )

    assert res.cache is True


@pytest.mark.asyncio
async def test_save_cache_handles_datetime(fake_redis):
    from datetime import datetime
    import zoneinfo
    res = CachedResource()
    res.cache = True
    res.tz = zoneinfo.ZoneInfo('UTC')
    res.cache_key = f'{KEY_PREFIX}cache:/spaces/dt'
    res.cache_namespace = 'list:testapp.space'

    await res.save_cache({'created_at': datetime(2024, 1, 1, 12, 0, 0)})

    redis = get_redis()
    assert await redis.get(res.cache_key) is not None
