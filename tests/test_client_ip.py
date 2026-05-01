import importlib
import sys
from unittest.mock import patch

import pytest


class FakeRequest:
    def __init__(self, **meta):
        self.META = meta


def reload_with_trusted(proxies):
    """Re-import client_ip with patched TRUSTED_PROXIES."""
    sys.modules.pop('zeromcp.client_ip', None)
    with patch.dict('sys.modules'):
        with patch('settings.settings') as fake_settings:
            fake_settings.TRUSTED_PROXIES = proxies
            return importlib.import_module('zeromcp.client_ip')


def test_no_trusted_proxies_uses_remote_addr_only(monkeypatch):
    from zeromcp import client_ip
    monkeypatch.setattr(client_ip, '_trusted_networks', [])
    req = FakeRequest(REMOTE_ADDR='1.2.3.4', HTTP_X_REAL_IP='99.99.99.99')
    assert client_ip.get_client_ip(req) == '1.2.3.4'


def test_untrusted_proxy_ignores_x_real_ip(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(REMOTE_ADDR='1.2.3.4', HTTP_X_REAL_IP='99.99.99.99')
    assert client_ip.get_client_ip(req) == '1.2.3.4'


def test_trusted_proxy_honours_x_real_ip(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(REMOTE_ADDR='10.0.0.5', HTTP_X_REAL_IP='99.99.99.99')
    assert client_ip.get_client_ip(req) == '99.99.99.99'


def test_trusted_proxy_honours_x_forwarded_for_first(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(
        REMOTE_ADDR='10.0.0.5',
        HTTP_X_FORWARDED_FOR='99.99.99.99, 10.0.0.5',
    )
    assert client_ip.get_client_ip(req) == '99.99.99.99'


def test_trusted_proxy_no_forward_headers_falls_back(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(REMOTE_ADDR='10.0.0.5')
    assert client_ip.get_client_ip(req) == '10.0.0.5'


def test_invalid_remote_addr_not_trusted(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(REMOTE_ADDR='garbage', HTTP_X_REAL_IP='99.99.99.99')
    assert client_ip.get_client_ip(req) == 'garbage'


def test_x_forwarded_for_empty_falls_to_x_real_ip(monkeypatch):
    import ipaddress
    from zeromcp import client_ip
    monkeypatch.setattr(
        client_ip, '_trusted_networks', [ipaddress.ip_network('10.0.0.0/8')]
    )
    req = FakeRequest(
        REMOTE_ADDR='10.0.0.5',
        HTTP_X_FORWARDED_FOR='',
        HTTP_X_REAL_IP='99.99.99.99',
    )
    assert client_ip.get_client_ip(req) == '99.99.99.99'
