"""CSRF hardening (Origin header) and cache footgun warning."""
import logging

import pytest

from zeromcp import base as base_mod
from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


# ── #3 — get_allowed_domain prefers Origin over Referer ───────────────


class _Probe(BaseResource):
    model = Space


def _request(headers):
    class R:
        pass
    r = R()
    r.headers = headers
    return r


def test_localhost_passes(monkeypatch):
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['app.example.com'])
    res = _Probe()
    res.get_allowed_domain(_request({'Host': 'localhost'}))


def test_origin_matches_allowlist(monkeypatch):
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['example.com'])
    res = _Probe()
    res.get_allowed_domain(_request({
        'Host': 'app.example.com',
        'Origin': 'https://app.example.com',
    }))


def test_origin_takes_precedence_over_referer(monkeypatch):
    """Browser-set Origin must win — a malicious page can sometimes
    strip Referer but not forge a different Origin."""
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['example.com'])
    res = _Probe()
    # Origin valid, Referer would be valid too — confirms Origin path
    res.get_allowed_domain(_request({
        'Host': 'app.example.com',
        'Origin': 'https://app.example.com',
        'Referer': 'https://evil.com/',
    }))


def test_origin_outside_allowlist_blocked(monkeypatch):
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['example.com'])
    res = _Probe()
    with pytest.raises(HTTPException) as exc:
        res.get_allowed_domain(_request({
            'Host': 'app.example.com',
            'Origin': 'https://evil.com',
        }))
    assert exc.value.args[0] == 403


def test_referer_fallback_when_no_origin(monkeypatch):
    """Older clients / same-origin GETs may omit Origin; Referer is
    still honored."""
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['example.com'])
    res = _Probe()
    res.get_allowed_domain(_request({
        'Host': 'app.example.com',
        'Referer': 'https://app.example.com/page',
    }))


def test_no_origin_no_referer_blocked(monkeypatch):
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', ['example.com'])
    res = _Probe()
    with pytest.raises(HTTPException) as exc:
        res.get_allowed_domain(_request({
            'Host': 'app.example.com',
        }))
    assert exc.value.args[0] == 403


def test_allowed_origins_unset_skips_check(monkeypatch):
    monkeypatch.setattr(base_mod, 'ALLOWED_ORIGINS', [])
    res = _Probe()
    # No raise — opt-in feature, off when not configured.
    res.get_allowed_domain(_request({'Host': 'whatever'}))


# ── #4 — cache footgun warning semantics live in test_cache_warning ───


def test_cache_without_scoping_no_longer_warns_at_class_declaration(caplog):
    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        class Unsafe(BaseResource):
            model = Space
            cache = True
            authenticated = True
    assert not any('Unsafe' in record.message for record in caplog.records)


def test_cache_with_session_cache_does_not_warn_at_class_declaration(caplog):
    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        class Safe(BaseResource):
            model = Space
            cache = True
            authenticated = True
            session_cache = True
    assert not any(
        'Safe' in record.message and 'cache' in record.message
        for record in caplog.records
    )


def test_cache_with_scope_fields_does_not_warn_at_class_declaration(caplog):
    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        class ScopedByUser(BaseResource):
            model = Space
            cache = True
            authenticated = True
            cache_scope_fields = ('user.id',)
    assert not any(
        'ScopedByUser' in record.message
        for record in caplog.records
    )


def test_anonymous_cache_does_not_warn_at_class_declaration(caplog):
    """Public endpoints don't have a per-user scope concept — no need
    to warn."""
    with caplog.at_level(logging.WARNING, logger='zeromcp.base'):
        class PublicCached(BaseResource):
            model = Space
            cache = True
            authenticated = False
    assert not any(
        'PublicCached' in record.message
        for record in caplog.records
    )
