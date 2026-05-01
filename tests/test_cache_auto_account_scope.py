"""Automatic tenant isolation in the cache key.

Multi-tenant deployments share Redis. Without folding ``account_id``
into the cache key, two tenants hitting the same path+querystring
collide on the same key and the first response served to one tenant
leaks to the next. The framework auto-folds ``self.account_id`` so
projects don't need to repeat ``cache_scope_fields=[('account', 'id')]``
on every resource (and don't risk forgetting on a new one).
"""
import pytest
from django.test import RequestFactory

from zeromcp import base as base_module
from zeromcp.base import BaseResource
from tests.testapp.models import Space


class _Resource(BaseResource):
    model = Space
    authenticated = False
    cache = True


def _make(cls=_Resource, account_id=None, user=None, account=None):
    res = cls()
    res.user = user
    res.account = account
    res.account_id = account_id
    return res


def _request(path='/spaces', qs=''):
    return RequestFactory().get(f'{path}?{qs}' if qs else path)


# ── core behaviour ─────────────────────────────────────────────────


def test_different_tenants_yield_different_keys():
    a = _make(account_id=1)
    b = _make(account_id=2)
    key_a = a._build_cache_key(_request(), session_key=None)
    key_b = b._build_cache_key(_request(), session_key=None)
    assert key_a != key_b
    assert ':a=1:' in key_a or key_a.endswith(':a=1') or 'a=1' in key_a
    assert 'a=2' in key_b


def test_anonymous_request_keeps_legacy_shape():
    res = _make(account_id=None)
    key = res._build_cache_key(_request(), None)
    assert 'a=' not in key


def test_account_id_zero_still_folds():
    """``is not None`` semantics: ``account_id = 0`` is a real value,
    not absence. Must produce a key segment."""
    res = _make(account_id=0)
    key = res._build_cache_key(_request(), None)
    assert 'a=0' in key


def test_account_id_string_works():
    """Some deployments use UUID/string account ids."""
    res = _make(account_id='b1c0a1ff-1234-4abc-8def-0123456789ab')
    key = res._build_cache_key(_request(), None)
    assert 'a=b1c0a1ff' in key


# ── opt-out ─────────────────────────────────────────────────────────


def test_setting_off_disables_auto_fold(monkeypatch):
    monkeypatch.setattr(base_module, 'AUTO_SCOPE_CACHE_BY_ACCOUNT', False)
    res = _make(account_id=42)
    key = res._build_cache_key(_request(), None)
    assert 'a=' not in key


def test_setting_off_does_not_break_anonymous(monkeypatch):
    monkeypatch.setattr(base_module, 'AUTO_SCOPE_CACHE_BY_ACCOUNT', False)
    res = _make(account_id=None)
    key = res._build_cache_key(_request(), None)
    assert 'a=' not in key


# ── coexistence with existing cache features ─────────────────────────


def test_coexists_with_session_cache():
    class _R(_Resource):
        session_cache = True
    res = _make(_R, account_id=42)
    key = res._build_cache_key(_request(), session_key='sid-abc')
    assert 'sid-abc' in key
    assert 'a=42' in key


def test_coexists_with_cache_scope_fields():
    class _R(_Resource):
        cache_scope_fields = ['space_id']
    res = _make(
        _R, account_id=42, user={'id': 1, 'space_id': 7},
    )
    key = res._build_cache_key(_request(), None)
    assert 'a=42' in key
    assert 'scope=' in key


def test_redundant_scope_account_id_does_not_break():
    """Declaring ``('account', 'id')`` in cache_scope_fields is now an
    anti-pattern (auto-fold already handles it) but must not crash."""
    class _R(_Resource):
        cache_scope_fields = [('account', 'id')]
    res = _make(_R, account_id=42, user={'id': 1}, account={'id': 42})
    key = res._build_cache_key(_request(), None)
    assert 'a=42' in key
    assert 'scope=' in key


# ── helper contract ──────────────────────────────────────────────────


def test_account_segment_returns_none_for_anonymous():
    assert _make(account_id=None)._account_cache_segment() is None


def test_account_segment_returns_segment_when_active():
    assert _make(account_id=99)._account_cache_segment() == 'a=99'


def test_account_segment_respects_setting(monkeypatch):
    monkeypatch.setattr(base_module, 'AUTO_SCOPE_CACHE_BY_ACCOUNT', False)
    assert _make(account_id=99)._account_cache_segment() is None


def test_account_segment_handles_zero():
    """``account_id=0`` is folded as ``a=0`` (real value, not absence)."""
    assert _make(account_id=0)._account_cache_segment() == 'a=0'
