"""Warnings for authenticated response cache without enough isolation."""
import logging

import pytest

from zeromcp import base as base_module
from zeromcp.base import BaseResource, _WARNED_UNSCOPED_AUTH_CACHE
from tests.testapp.models import Space


class _CachedAuthResource(BaseResource):
    model = Space
    authenticated = True
    cache = True


@pytest.fixture(autouse=True)
def reset_warning_registry():
    _WARNED_UNSCOPED_AUTH_CACHE.clear()
    yield
    _WARNED_UNSCOPED_AUTH_CACHE.clear()


def _make(cls=_CachedAuthResource, account_id=None):
    res = cls()
    res.account_id = account_id
    return res


def test_no_warning_when_account_auto_scope_active(caplog):
    res = _make(account_id=42)

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()

    assert not [r for r in caplog.records if r.name == 'zeromcp.base']


def test_warning_when_no_account_scope_for_request(caplog):
    res = _make(account_id=None)

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()

    records = [r for r in caplog.records if r.name == 'zeromcp.base']
    assert records
    msg = records[0].getMessage()
    assert 'without session_cache or cache_scope_fields' in msg
    assert 'no active account auto-scope' in msg


def test_warning_when_account_auto_scope_disabled(caplog, monkeypatch):
    monkeypatch.setattr(base_module, 'AUTO_SCOPE_CACHE_BY_ACCOUNT', False)
    res = _make(account_id=42)

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()

    records = [r for r in caplog.records if r.name == 'zeromcp.base']
    assert records


def test_no_warning_when_session_cache_enabled(caplog):
    class _SessionScoped(_CachedAuthResource):
        session_cache = True

    res = _SessionScoped()
    res.account_id = None

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()

    assert not [r for r in caplog.records if r.name == 'zeromcp.base']


def test_no_warning_when_explicit_scope_fields_present(caplog):
    class _ExplicitScope(_CachedAuthResource):
        cache_scope_fields = ['id']

    res = _ExplicitScope()
    res.account_id = None

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()

    assert not [r for r in caplog.records if r.name == 'zeromcp.base']


def test_warning_emitted_once_per_context(caplog):
    res = _make(account_id=None)

    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        res._warn_if_authenticated_cache_unscoped()
        res._warn_if_authenticated_cache_unscoped()

    records = [r for r in caplog.records if r.name == 'zeromcp.base']
    assert len(records) == 1
