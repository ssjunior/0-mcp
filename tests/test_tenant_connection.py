import json
from unittest.mock import MagicMock

import pytest

from zeromcp.redis_config import get_redis
from zeromcp.tenant import tenant


def _fake_account(id, host='db.local', user='u', password='p'):
    acc = MagicMock()
    acc.id = id
    acc.db.host = host
    acc.db.user = user
    acc.db.password = password
    return acc


def test_build_connection_shape():
    acc = _fake_account(42, host='h', user='u', password='p')
    conn = tenant._build_connection('tenant_42', acc)

    assert conn['NAME'] == 'tenant_42'
    assert conn['HOST'] == 'h'
    assert conn['USER'] == 'u'
    assert conn['PASSWORD'] == 'p'
    assert conn['ENGINE'] == 'django.db.backends.mysql'
    assert conn['OPTIONS']['charset'] == 'utf8mb4'


@pytest.mark.asyncio
async def test_load_connection_caches_in_redis(fake_redis):
    acc = _fake_account(7)
    conn = await tenant._load_connection(7, acc)
    assert conn['NAME'] == f'{tenant.TENANT_DB_PREFIX}_7'

    redis = get_redis()
    raw = await redis.get(f'{tenant.TENANT_DB_PREFIX}:connections:7')
    assert raw is not None
    assert json.loads(raw)['HOST'] == 'db.local'


@pytest.mark.asyncio
async def test_load_connection_hits_cache_second_time(fake_redis, monkeypatch):
    acc = _fake_account(8)
    await tenant._load_connection(8, acc)

    # Second call must NOT hit account_model.objects (cache hit).
    fail_account_model = MagicMock()
    fail_account_model.objects.filter.side_effect = AssertionError(
        'should not query DB on cache hit'
    )
    monkeypatch.setattr(tenant, 'account_model', fail_account_model)

    conn = await tenant._load_connection(8)
    assert conn['NAME'] == f'{tenant.TENANT_DB_PREFIX}_8'
