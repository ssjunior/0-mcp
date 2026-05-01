"""Per-scope cache keying via ``cache_scope_fields``.

Folds dimensions like ``role_id`` or ``space_id`` into the cache key so
users sharing the same scope share the cache, while different scopes
get isolated keys. Strict semantics: missing field on an authenticated
user raises HTTPException(500) instead of silently merging caches.
"""
import pytest
from django.test import RequestFactory

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


class _Resource(BaseResource):
    model = Space
    authenticated = False
    cache = True


def _make(cls=_Resource, user=None, account=None):
    res = cls()
    res.user = user
    res.account = account
    return res


def _request(path='/spaces', qs=''):
    return RequestFactory().get(f'{path}?{qs}' if qs else path)


# ── normalization (validated at __init__) ──────────────────────────


def test_str_shorthand_normalizes_to_user_source():
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    res = _R()
    assert res.cache_scope_fields == [('user', 'space_id')]


def test_tuple_form_accepted():
    class _R(_Resource):
        cache_scope_fields = [('account', 'plan_id')]
    res = _R()
    assert res.cache_scope_fields == [('account', 'plan_id')]


def test_mixed_str_and_tuple():
    class _R(_Resource):
        cache_scope_fields = ['space_id', ('account', 'plan_id')]
    res = _R()
    assert res.cache_scope_fields == [
        ('user', 'space_id'), ('account', 'plan_id'),
    ]


def test_invalid_source_raises_at_init():
    class _R(_Resource):
        cache_scope_fields = [('session', 'foo')]
    with pytest.raises(ValueError, match='Invalid cache_scope_fields source'):
        _R()


def test_invalid_entry_shape_raises_at_init():
    class _R(_Resource):
        cache_scope_fields = [123]
    with pytest.raises(ValueError, match='Invalid cache_scope_fields entry'):
        _R()


def test_empty_or_none_is_noop():
    class _R(_Resource):
        cache_scope_fields = None
    assert _R().cache_scope_fields == []

    class _R2(_Resource):
        cache_scope_fields = []
    assert _R2().cache_scope_fields == []


# ── key derivation ─────────────────────────────────────────────────


def test_no_scope_keeps_legacy_key_shape():
    res = _make()
    key = res._build_cache_key(_request(), session_key=None)
    assert 'scope=' not in key


def test_anonymous_user_skips_scope_fold():
    """authenticated=False resources may still set scope; if there's no
    user/account in the request, fold is skipped silently."""
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    res = _make(_R, user=None, account=None)
    key = res._build_cache_key(_request(), session_key=None)
    assert 'scope=' not in key


def test_same_scope_yields_same_key():
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    a = _make(_R, user={'id': 1, 'space_id': 7})
    b = _make(_R, user={'id': 2, 'space_id': 7})
    assert a._build_cache_key(_request(), None) == b._build_cache_key(_request(), None)


def test_different_scope_yields_different_keys():
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    a = _make(_R, user={'id': 1, 'space_id': 7})
    b = _make(_R, user={'id': 1, 'space_id': 8})
    assert a._build_cache_key(_request(), None) != b._build_cache_key(_request(), None)


def test_falsy_values_are_present_not_missing():
    """None / 0 / '' are valid scope values — not the same as absence."""
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    null_user = _make(_R, user={'id': 1, 'space_id': None})
    zero_user = _make(_R, user={'id': 1, 'space_id': 0})
    empty_user = _make(_R, user={'id': 1, 'space_id': ''})

    keys = {
        null_user._build_cache_key(_request(), None),
        zero_user._build_cache_key(_request(), None),
        empty_user._build_cache_key(_request(), None),
    }
    # All three folded successfully (no exception) and produced
    # distinct keys (None ≠ 0 ≠ '').
    assert len(keys) == 3


def test_account_source_folded():
    class _R(_Resource):
        cache_scope_fields = [('account', 'plan_id')]
    res = _make(_R, user={'id': 1}, account={'id': 9, 'plan_id': 'pro'})
    key_pro = res._build_cache_key(_request(), None)

    res2 = _make(_R, user={'id': 1}, account={'id': 9, 'plan_id': 'free'})
    key_free = res2._build_cache_key(_request(), None)

    assert 'scope=' in key_pro
    assert key_pro != key_free


def test_multiple_dimensions_combine():
    class _R(_Resource):
        cache_scope_fields = ['space_id', ('account', 'plan_id')]
    a = _make(_R, user={'space_id': 7}, account={'plan_id': 'pro'})
    b = _make(_R, user={'space_id': 7}, account={'plan_id': 'free'})
    assert a._build_cache_key(_request(), None) != b._build_cache_key(_request(), None)


# ── lenient semantics (warn + skip fold, never 500) ────────────────


def test_missing_field_disables_cache(caplog):
    """Missing scope field on authenticated request must disable cache
    for that request — a shared key across users would silently leak
    data across whatever dimension the operator was trying to protect.
    The 500 strict mode was too aggressive (took the site down); just
    skipping the fold and writing to a shared key was too permissive."""
    import logging as _logging

    class _R(_Resource):
        cache_scope_fields = ['space_id']
    res = _make(_R, user={'id': 1})  # space_id NOT in user
    assert res.cache is True  # configured

    with caplog.at_level(_logging.WARNING, logger='zeromcp.base'):
        key = res._build_cache_key(_request(), None)

    # Fold skipped AND cache disabled for this request
    assert 'scope=' not in key
    assert res.cache is False, (
        'cache must be disabled when scope is configured but unresolved'
    )

    records = [r for r in caplog.records if r.name == 'zeromcp.base']
    assert records
    msg = records[0].getMessage()
    assert 'space_id' in msg
    assert 'disabling cache' in msg


def test_missing_account_source_disables_cache(caplog):
    import logging as _logging

    class _R(_Resource):
        cache_scope_fields = [('account', 'plan_id')]
    res = _make(_R, user={'id': 1}, account=None)
    assert res.cache is True

    with caplog.at_level(_logging.WARNING, logger='zeromcp.base'):
        key = res._build_cache_key(_request(), None)

    assert 'scope=' not in key
    assert res.cache is False
    records = [r for r in caplog.records if r.name == 'zeromcp.base']
    assert records
    assert 'account' in records[0].getMessage()


def test_resolved_scope_keeps_cache_enabled():
    """Sanity: when scope resolves cleanly, cache stays enabled."""
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    res = _make(_R, user={'id': 1, 'space_id': 7})
    assert res.cache is True

    key = res._build_cache_key(_request(), None)

    assert 'scope=' in key
    assert res.cache is True


# ── coexistence with session_cache ─────────────────────────────────


def test_coexists_with_session_cache():
    class _R(_Resource):
        session_cache = True
        cache_scope_fields = ['space_id']
    res = _make(_R, user={'id': 1, 'space_id': 7})
    key = res._build_cache_key(_request(), session_key='sid-abc')
    assert 'sid-abc' in key
    assert 'scope=' in key
