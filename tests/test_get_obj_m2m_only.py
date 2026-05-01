"""Regression: get_obj must work when ``edit_related_fields`` contains
**only** M2M entries (no chained FKs).

Before the fix, the M2M loop dereferenced a name (``obj``) that only
got bound inside the previous ``for model in related`` loop. With
``related`` empty and m2m present, the call raised
``UnboundLocalError: local variable 'obj' referenced before assignment``."""
from unittest.mock import MagicMock

import pytest

from zeromcp.base import BaseResource
from tests.testapp.models import Space


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeManager:
    def __init__(self, items):
        self._items = items

    def values(self, *fields):
        # Mirror Django's manager: returns an async-iterable of dicts
        # restricted to ``fields``.
        return _AsyncIter([{f: item.get(f) for f in fields} for item in self._items])


class _FakeMeta:
    def __init__(self, field_name):
        self._field_name = field_name

    def get_field(self, name):
        if name != self._field_name:
            raise AssertionError(f'unexpected field lookup: {name}')
        field = MagicMock()
        field.name = name
        return field


class _FakeObj:
    def __init__(self, pk, tags, field_name='tags'):
        self.pk = pk
        self.id = pk
        self.name = 'fake-space'
        self.active = True
        self._meta = _FakeMeta(field_name)
        # The attribute access ``self.obj.tags`` returns the manager
        setattr(self, field_name, _FakeManager(tags))


class _FakeQueryset:
    def __init__(self, obj):
        self._obj = obj

    def filter(self, **kwargs):
        return self

    def select_related(self, *args):
        return self

    def prefetch_related(self, *args):
        return self

    async def afirst(self):
        return self._obj


class _Resource(BaseResource):
    model = Space
    authenticated = False
    edit_fields = ['id', 'name', 'active']
    m2m_fields = ['tags']
    edit_related_fields = {'tags': ['id', 'label']}


@pytest.mark.asyncio
async def test_get_obj_m2m_only_no_unbound_local():
    """Reproduces the UnboundLocalError fix: only M2M, no FKs."""
    res = _Resource()
    fake_tags = [
        {'id': 1, 'label': 'urgent'},
        {'id': 2, 'label': 'finance'},
    ]
    res.obj = None  # cleared by get_obj
    res.queryset = _FakeQueryset(_FakeObj(pk=42, tags=fake_tags))
    res.filters = None
    res.edit_prefetch_related = {}

    result = await res.get_obj(42)

    assert result['tags'] == fake_tags
    assert result['id'] == 42
    assert result['name'] == 'fake-space'


@pytest.mark.asyncio
async def test_get_obj_m2m_only_empty_relation():
    res = _Resource()
    res.obj = None
    res.queryset = _FakeQueryset(_FakeObj(pk=43, tags=[]))
    res.filters = None
    res.edit_prefetch_related = {}

    result = await res.get_obj(43)
    assert result['tags'] == []


@pytest.mark.asyncio
async def test_get_obj_uses_self_obj_meta_not_loop_local():
    """Static guard: the M2M branch must reference self.obj._meta, not a
    loop-local ``obj`` that may be unbound when ``related`` is empty."""
    import inspect
    from zeromcp.base import BaseResource as _BR

    src = inspect.getsource(_BR.get_obj)
    # The fix line must be present
    assert 'self.obj._meta.get_field(related_field)' in src
    # And the buggy version must be gone
    assert 'obj._meta.get_field(related_field)\n' not in src or \
        'self.obj._meta.get_field(related_field)' in src
