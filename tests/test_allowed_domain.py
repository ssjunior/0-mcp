from types import SimpleNamespace

import pytest

from zeromcp import base
from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException


def _req(headers):
    return SimpleNamespace(headers=headers)


def test_get_allowed_domain_noop_when_origins_empty(monkeypatch):
    monkeypatch.setattr(base, 'ALLOWED_ORIGINS', [])
    BaseResource().get_allowed_domain(_req({}))


def test_get_allowed_domain_missing_host_raises_403(monkeypatch):
    monkeypatch.setattr(base, 'ALLOWED_ORIGINS', ['example.com'])
    with pytest.raises(HTTPException) as exc:
        BaseResource().get_allowed_domain(_req({}))
    assert exc.value.args[0] == 403


def test_get_allowed_domain_unknown_host_raises_403(monkeypatch):
    monkeypatch.setattr(base, 'ALLOWED_ORIGINS', ['example.com'])
    with pytest.raises(HTTPException) as exc:
        BaseResource().get_allowed_domain(
            _req({'Host': 'evil.com', 'Referer': 'https://evil.com/x'})
        )
    assert exc.value.args[0] == 403


def test_get_allowed_domain_referer_match(monkeypatch):
    monkeypatch.setattr(base, 'ALLOWED_ORIGINS', ['example.com'])
    BaseResource().get_allowed_domain(
        _req({'Host': 'api.other.com', 'Referer': 'https://app.example.com/x'})
    )


@pytest.mark.parametrize('host', [
    'localhost',
    'localhost:3000',
    '127.0.0.1',
    '127.0.0.1:5173',
])
def test_get_allowed_domain_allows_localhost(monkeypatch, host):
    monkeypatch.setattr(base, 'ALLOWED_ORIGINS', ['example.com'])
    BaseResource().get_allowed_domain(_req({'Host': host}))
