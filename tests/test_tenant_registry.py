"""TenantConnectionRegistry — encapsulates alias, Redis cache, and
``connections.databases`` mutation. Phase 1 refactor: same behavior as
before, all funneled through the facade."""
from unittest.mock import MagicMock

import pytest
from django.db import connections

from zeromcp.tenant import tenant
from zeromcp.tenant.registry import TenantConnectionRegistry


def _fake_account(id, host='db.local', user='u', password='p'):
    acc = MagicMock()
    acc.id = id
    acc.db.host = host
    acc.db.user = user
    acc.db.password = password
    return acc


def _make_registry(account_model=None):
    return TenantConnectionRegistry(
        db_prefix='tnt',
        default_database={'NAME': 'default-db'},
        account_model_getter=lambda: account_model,
    )


def test_make_alias_uses_prefix():
    reg = _make_registry()
    assert reg.make_alias(7) == 'tnt_7'


def test_make_alias_falsy_returns_default():
    reg = _make_registry()
    assert reg.make_alias(None) == 'default'
    assert reg.make_alias(0) == 'default'


@pytest.mark.asyncio
async def test_get_or_create_registers_alias_and_caches(fake_redis):
    reg = _make_registry()
    acc = _fake_account(101)
    alias, conn = await reg.get_or_create(101, acc)

    assert alias == 'tnt_101'
    assert connections.databases[alias]['HOST'] == 'db.local'
    assert conn['NAME'] == alias

    cached = await reg.get_cached_connection(101)
    assert cached is not None
    assert cached['HOST'] == 'db.local'

    del connections.databases[alias]


@pytest.mark.asyncio
async def test_get_or_create_second_call_hits_cache(fake_redis):
    fail_model = MagicMock()
    fail_model.objects.filter.side_effect = AssertionError(
        'cache hit must skip DB'
    )
    reg = _make_registry(account_model=fail_model)

    await reg.get_or_create(202, _fake_account(202))
    # second call without account, with account_model that explodes if hit
    alias, conn = await reg.get_or_create(202)

    assert alias == 'tnt_202'
    assert conn['HOST'] == 'db.local'

    del connections.databases['tnt_202']


@pytest.mark.asyncio
async def test_get_or_create_fetches_account_when_not_provided(fake_redis):
    fetched = _fake_account(303, host='fetched.db')

    async def afirst():
        return fetched

    qs = MagicMock()
    qs.afirst = afirst
    qs.select_related.return_value = qs

    model = MagicMock()
    model.objects.filter.return_value = qs

    reg = _make_registry(account_model=model)
    alias, conn = await reg.get_or_create(303)

    assert conn['HOST'] == 'fetched.db'
    assert alias in connections.databases

    del connections.databases[alias]


def test_is_registered_reflects_register_connection():
    reg = _make_registry()
    alias = reg.make_alias(404)
    assert not reg.is_registered(alias)

    reg.register_connection(alias, {'NAME': alias})
    assert reg.is_registered(alias)

    del connections.databases[alias]


@pytest.mark.asyncio
async def test_get_or_create_missing_account_raises_400(fake_redis):
    """When no ``account`` is supplied and the DB lookup also returns
    None, the registry must raise the same ``HTTPException(400, 'Missing
    account')`` that ``aset_tenant`` historically raised — otherwise
    callers like ``set_default`` blow up with an opaque AttributeError."""
    async def afirst():
        return None

    qs = MagicMock()
    qs.afirst = afirst
    qs.select_related.return_value = qs

    model = MagicMock()
    model.objects.filter.return_value = qs

    reg = _make_registry(account_model=model)

    from zeromcp.exception import HTTPException
    with pytest.raises(HTTPException) as exc:
        await reg.get_or_create(9999)
    assert exc.value.args[0] == 400
    assert 'Missing account' in exc.value.args[1]


@pytest.mark.asyncio
async def test_set_default_swaps_global_default(fake_redis):
    reg = _make_registry()
    await reg.get_or_create(505, _fake_account(505, host='tenant.db'))

    original_default = connections.databases.get('default')
    try:
        await reg.set_default_connection(505)
        assert connections.databases['default']['HOST'] == 'tenant.db'

        reg.restore_default_connection()
        assert connections.databases['default'] == {'NAME': 'default-db'}
    finally:
        if original_default is not None:
            connections.databases['default'] = original_default
        del connections.databases['tnt_505']


# ── tenant.py integration — wrappers still consume the registry ──────


@pytest.mark.asyncio
async def test_aset_tenant_registers_via_registry(fake_redis, monkeypatch):
    """aset_tenant must funnel through the module-level registry, not
    write to connections.databases directly."""
    captured = {}

    async def fake_get_or_create(account_id, account=None):
        captured['called_with'] = (account_id, account)
        alias = tenant.registry.make_alias(account_id)
        connections.databases[alias] = {'NAME': alias}
        return alias, connections.databases[alias]

    monkeypatch.setattr(tenant.registry, 'get_or_create', fake_get_or_create)

    fake_account = _fake_account(606)

    async def afirst():
        return fake_account

    qs = MagicMock()
    qs.afirst = afirst
    qs.select_related.return_value = qs

    model = MagicMock()
    model.objects.filter.return_value = qs
    monkeypatch.setattr(tenant, 'account_model', model)

    alias = await tenant.aset_tenant(606)

    assert alias == tenant.registry.make_alias(606)
    assert tenant.db_state.get() == alias
    assert captured['called_with'][0] == 606

    del connections.databases[alias]


@pytest.mark.asyncio
async def test_aset_tenant_skips_fetch_when_alias_already_registered(fake_redis, monkeypatch):
    alias = tenant.registry.make_alias(707)
    connections.databases[alias] = {'NAME': alias}

    fail_model = MagicMock()
    fail_model.objects.filter.side_effect = AssertionError(
        'must not query when alias already registered'
    )
    monkeypatch.setattr(tenant, 'account_model', fail_model)

    try:
        result = await tenant.aset_tenant(707)
        assert result == alias
        assert tenant.db_state.get() == alias
    finally:
        del connections.databases[alias]


@pytest.mark.asyncio
async def test_set_default_uses_registry(fake_redis, monkeypatch):
    called = {}

    async def fake_set_default(account_id):
        called['account_id'] = account_id

    monkeypatch.setattr(
        tenant.registry, 'set_default_connection', fake_set_default,
    )
    await tenant.set_default(808)
    assert called == {'account_id': 808}


@pytest.mark.asyncio
async def test_unset_default_uses_registry(fake_redis, monkeypatch):
    called = {}

    def fake_restore():
        called['restored'] = True

    monkeypatch.setattr(
        tenant.registry, 'restore_default_connection', fake_restore,
    )
    await tenant.unset_default(909)
    assert called == {'restored': True}


def test_get_tenant_handles_underscore_prefix(monkeypatch):
    """``TENANT_DB_PREFIX`` may contain underscores (``my_tenant``).
    ``get_tenant`` must extract the trailing id segment, not the second
    split chunk."""
    token = tenant.db_state.set('my_tenant_42')
    try:
        assert tenant.get_tenant() == '42'
    finally:
        tenant.db_state.reset(token)


def test_get_tenant_returns_none_for_default(monkeypatch):
    token = tenant.db_state.set('default')
    try:
        assert tenant.get_tenant() is None
    finally:
        tenant.db_state.reset(token)


def test_no_module_writes_connections_databases_outside_registry():
    """Static guard: only registry.py and db_router-touching code may
    mutate connections.databases. tenant.py should be clean now."""
    import inspect

    src = inspect.getsource(tenant)
    # The wrappers must not assign to connections.databases anymore.
    assert 'connections.databases[' not in src, (
        'tenant.py must funnel all connections.databases mutation through '
        'TenantConnectionRegistry'
    )
    assert "connections.databases['default']" not in src
