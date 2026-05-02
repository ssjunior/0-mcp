"""Authorization regression tests — owner_field on read + write paths.

Cover the bugs where:
- ``update_obj`` looked up rows by pk only, ignoring ``owner_field``.
- ``create_obj`` accepted ``owner_id`` from the client, allowing a user
  to forge ownership on creation.
- ``get_obj`` / ``get_objs`` did not honor ``owner_field``, so a user
  could read another user's rows by id or via the list endpoint, even
  though the resource declared ownership.

These tests mock the queryset / manager so the assertions focus on the
filter kwargs the framework passes — not on real DB I/O.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.db import models
from django.test.utils import isolate_apps

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space, User


# ── #1 — update_obj must filter by owner ──────────────────────────────


class _OwnedUpdate(BaseResource):
    model = Space
    owner_field = 'owner_id'
    update_fields = ['name']
    fields = ['id', 'name']
    all_fields = ['id', 'name']


@pytest.mark.asyncio
async def test_update_obj_fetch_uses_owner_filter():
    """Vítima de A: PATCH em recurso de B com id conhecido."""
    res = _OwnedUpdate()
    res.user = {'id': 42}
    res.diff = {}

    aget = AsyncMock(side_effect=Exception('not owned'))
    res.queryset = MagicMock()
    res.queryset.aget = aget

    with pytest.raises(HTTPException) as exc:
        await res.update_obj(id=99, body={'name': 'pwn'})
    assert exc.value.args[0] == 404
    aget.assert_awaited_once_with(pk=99, owner_id=42)


@pytest.mark.asyncio
async def test_update_obj_aupdate_uses_owner_filter(monkeypatch):
    """Mesmo que aget passe (mocked), o aupdate final precisa filtrar
    por owner — defesa em profundidade contra divergência entre fetch
    e write."""
    res = _OwnedUpdate()
    res.user = {'id': 42}
    res.diff = {}

    obj = MagicMock()
    obj.name = 'before'
    res.queryset = MagicMock()
    res.queryset.aget = AsyncMock(return_value=obj)

    aupdate = AsyncMock(return_value=1)
    captured = {}

    def filter_(**kwargs):
        captured['kwargs'] = kwargs
        m = MagicMock()
        m.aupdate = aupdate
        return m

    fake_manager = MagicMock()
    fake_manager.filter.side_effect = filter_

    async def _get_obj_stub(_id):
        return {'id': _id, 'name': 'after'}
    res.get_obj = _get_obj_stub

    monkeypatch.setattr(res.model, 'objects', fake_manager)
    await res.update_obj(id=99, body={'name': 'after'})
    assert captured['kwargs'] == {'pk': 99, 'owner_id': 42}
    aupdate.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_obj_without_owner_field_no_filter():
    """Resource without owner_field must not inject any extra kwarg."""
    class Plain(BaseResource):
        model = Space
        update_fields = ['name']
        fields = ['id', 'name']
        all_fields = ['id', 'name']

    res = Plain()
    res.user = {'id': 42}
    res.diff = {}
    aget = AsyncMock(side_effect=Exception('boom'))
    res.queryset = MagicMock()
    res.queryset.aget = aget

    with pytest.raises(HTTPException):
        await res.update_obj(id=1, body={'name': 'x'})
    aget.assert_awaited_once_with(pk=1)


# ── #2 — create_obj must force owner_id ───────────────────────────────


class _OwnedCreate(BaseResource):
    model = Space
    create_fields = ['name', 'owner_id']
    fields = ['id', 'name', 'owner_id']
    all_fields = ['id', 'name', 'owner', 'owner_id']

@isolate_apps('tests.testapp')
@pytest.mark.asyncio
async def test_create_obj_forces_owner_id_over_client_value(monkeypatch):
    """Cliente envia owner_id=99 (vítima); framework substitui por
    user['id']=42 antes de salvar."""
    class OwnedSpace(models.Model):
        name = models.CharField(max_length=100)
        owner = models.ForeignKey(User, null=True, on_delete=models.CASCADE)

        class Meta:
            app_label = 'testapp'
            managed = False

    class OwnedCreate(BaseResource):
        model = OwnedSpace
        create_fields = ['name', 'owner_id']

    res = OwnedCreate()
    res.user = {'id': 42}
    captured = {}

    async def fake_acreate(**kwargs):
        captured.update(kwargs)
        obj = MagicMock()
        obj.id = 1
        return obj

    monkeypatch.setattr(
        OwnedSpace, 'objects', SimpleNamespace(acreate=fake_acreate),
    )
    res.get_obj = AsyncMock(return_value={'id': 1})
    res.return_result = AsyncMock(side_effect=lambda x: x)

    await res.create_obj(
        request=MagicMock(), body={'name': 'x', 'owner_id': 99},
    )
    assert captured['owner_id'] == 42


@isolate_apps('tests.testapp')
@pytest.mark.asyncio
async def test_create_obj_allow_owner_override_preserves_client_value(monkeypatch):
    """Opt-in: admin path quer criar em nome de outro usuário."""
    class OwnedSpace(models.Model):
        name = models.CharField(max_length=100)
        owner = models.ForeignKey(User, null=True, on_delete=models.CASCADE)

        class Meta:
            app_label = 'testapp'
            managed = False

    class OwnedCreate(BaseResource):
        model = OwnedSpace
        create_fields = ['name', 'owner_id']
        allow_owner_override = True

    res = OwnedCreate()
    res.user = {'id': 42}
    captured = {}

    async def fake_acreate(**kwargs):
        captured.update(kwargs)
        obj = MagicMock()
        obj.id = 1
        return obj

    monkeypatch.setattr(
        OwnedSpace, 'objects', SimpleNamespace(acreate=fake_acreate),
    )
    res.get_obj = AsyncMock(return_value={'id': 1})
    res.return_result = AsyncMock(side_effect=lambda x: x)

    await res.create_obj(
        request=MagicMock(), body={'name': 'x', 'owner_id': 99},
    )
    assert captured['owner_id'] == 99


@isolate_apps('tests.testapp')
@pytest.mark.asyncio
async def test_create_obj_defaults_owner_to_user_when_absent(monkeypatch):
    class OwnedSpace(models.Model):
        name = models.CharField(max_length=100)
        owner = models.ForeignKey(User, null=True, on_delete=models.CASCADE)

        class Meta:
            app_label = 'testapp'
            managed = False

    class OwnedCreate(BaseResource):
        model = OwnedSpace
        create_fields = ['name', 'owner_id']

    res = OwnedCreate()
    res.user = {'id': 42}
    captured = {}

    async def fake_acreate(**kwargs):
        captured.update(kwargs)
        obj = MagicMock()
        obj.id = 1
        return obj

    monkeypatch.setattr(
        OwnedSpace, 'objects', SimpleNamespace(acreate=fake_acreate),
    )
    res.get_obj = AsyncMock(return_value={'id': 1})
    res.return_result = AsyncMock(side_effect=lambda x: x)

    await res.create_obj(request=MagicMock(), body={'name': 'x'})
    assert captured['owner_id'] == 42


# ── #5 — get_obj / get_objs honor owner_field ─────────────────────────


class _OwnedRead(BaseResource):
    model = Space
    owner_field = 'owner_id'
    edit_fields = ['id', 'name']
    fields = ['id', 'name']
    all_fields = ['id', 'name']


@pytest.mark.asyncio
async def test_get_obj_filters_by_owner():
    """Vítima de A tenta GET /resource/<id> de B — 404, não vaza."""
    res = _OwnedRead()
    res.user = {'id': 42}

    captured = {}

    def filter_(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.afirst = AsyncMock(return_value=None)
        return m

    qs = MagicMock()
    qs.filter.side_effect = filter_
    res.queryset = qs

    with pytest.raises(HTTPException) as exc:
        await res.get_obj(id=99)
    assert exc.value.args[0] == 404
    # The owner_id kwarg must be present on the filter call
    assert captured == {'pk': 99, 'owner_id': 42}


@pytest.mark.asyncio
async def test_get_obj_without_owner_field_no_owner_filter():
    class Plain(BaseResource):
        model = Space
        edit_fields = ['id', 'name']
        fields = ['id', 'name']
        all_fields = ['id', 'name']

    res = Plain()
    res.user = {'id': 42}
    captured = {}

    def filter_(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.afirst = AsyncMock(return_value=None)
        return m

    qs = MagicMock()
    qs.filter.side_effect = filter_
    res.queryset = qs

    with pytest.raises(HTTPException):
        await res.get_obj(id=1)
    assert captured == {'pk': 1}


@pytest.mark.asyncio
async def test_get_objs_filters_queryset_by_owner():
    """LIST endpoint must apply ownership filter to the queryset chain
    so users only see their own rows."""
    res = _OwnedRead()
    res.user = {'id': 42}
    res.page = 1
    res.limit = 25
    res.order_by = 'id'

    captured = {}

    qs_after_filter = MagicMock()
    qs_after_filter.order_by.return_value = qs_after_filter
    qs_after_filter.__getitem__ = MagicMock(return_value=qs_after_filter)
    async def _empty_aiter_after(self):
        if False:
            yield
    qs_after_filter.__aiter__ = _empty_aiter_after

    def filter_(**kwargs):
        captured.update(kwargs)
        return qs_after_filter

    qs = MagicMock()
    qs.filter.side_effect = filter_
    res.queryset = qs

    async def stub_get_filters(_request):
        return None
    res.get_filters = stub_get_filters

    request = MagicMock()
    request.GET = {}
    await res.get_objs(request)
    assert captured == {'owner_id': 42}


@pytest.mark.asyncio
async def test_get_objs_without_owner_field_does_not_filter():
    class Plain(BaseResource):
        model = Space
        list_fields = ['id', 'name']
        fields = ['id', 'name']
        all_fields = ['id', 'name']

    res = Plain()
    res.user = {'id': 42}
    res.page = 1
    res.limit = 25
    res.order_by = 'id'

    qs = MagicMock()
    # Capture whether .filter() was invoked at all.
    qs.filter = MagicMock()
    qs.order_by.return_value = qs
    qs.__getitem__ = MagicMock(return_value=qs)
    async def _empty_aiter(self):
        if False:
            yield
    qs.__aiter__ = _empty_aiter
    res.queryset = qs

    async def stub_get_filters(_request):
        return None
    res.get_filters = stub_get_filters

    request = MagicMock()
    request.GET = {}
    await res.get_objs(request)
    qs.filter.assert_not_called()
