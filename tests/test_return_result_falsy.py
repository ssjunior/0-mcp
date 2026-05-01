"""Regression: return_result must drop fields excluded by edit_fields /
edit_exclude_fields **including falsy values** (0, False, '', None).

Before the fix, ``if result.get(key): del result[key]`` silently kept
falsy values, leaking fields like ``is_admin=False`` or ``count=0``."""
import pytest

from zeromcp.base import BaseResource
from tests.testapp.models import Space


class _Resource(BaseResource):
    model = Space
    edit_fields = ['id', 'name']
    edit_exclude_fields = ['secret']
    authenticated = False


@pytest.mark.asyncio
async def test_falsy_value_outside_edit_fields_is_dropped():
    res = _Resource()
    result = {
        'id': 1,
        'name': 'demo',
        'leak_zero': 0,
        'leak_false': False,
        'leak_empty': '',
        'leak_none': None,
    }
    out = await res.return_result(result)
    assert 'leak_zero' not in out
    assert 'leak_false' not in out
    assert 'leak_empty' not in out
    assert 'leak_none' not in out
    assert out['id'] == 1
    assert out['name'] == 'demo'


@pytest.mark.asyncio
async def test_falsy_value_in_edit_exclude_fields_is_dropped():
    res = _Resource()
    out = await res.return_result({
        'id': 1,
        'name': 'demo',
        'secret': '',          # explicitly excluded but falsy
    })
    assert 'secret' not in out


@pytest.mark.asyncio
async def test_truthy_value_in_edit_exclude_fields_still_dropped():
    res = _Resource()
    out = await res.return_result({
        'id': 1,
        'name': 'demo',
        'secret': 'leaked-token',
    })
    assert 'secret' not in out


@pytest.mark.asyncio
async def test_kept_fields_preserve_falsy_values():
    """Falsy values on whitelisted fields must NOT be stripped."""
    class _Res(BaseResource):
        model = Space
        edit_fields = ['id', 'active', 'count']
        authenticated = False

    res = _Res()
    out = await res.return_result({
        'id': 1,
        'active': False,
        'count': 0,
    })
    assert out['active'] is False
    assert out['count'] == 0


@pytest.mark.asyncio
async def test_protected_keys_pass_through():
    """``_result`` and ``custom`` are sentinels — never stripped, even
    if not in edit_fields."""
    res = _Resource()
    out = await res.return_result({
        'id': 1,
        'name': 'demo',
        '_result': {'meta': 1},
        'custom': {'extra': 1},
    })
    assert out['_result'] == {'meta': 1}
    assert out['custom'] == {'extra': 1}
