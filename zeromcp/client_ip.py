"""Resolve the real client IP behind a (possibly chained) reverse proxy.

X-Real-IP / X-Forwarded-For are user-controlled. Trusting them blindly lets
an attacker rotate the spoofed value to bypass IP-based rate limits and
abuse detection. This module honours a configured allowlist of trusted
proxies and only consults the headers when REMOTE_ADDR matches one.
"""
import ipaddress

from .settings_helper import get_setting

TRUSTED_PROXIES = get_setting('TRUSTED_PROXIES', default=[])


_trusted_networks = []
for entry in TRUSTED_PROXIES or []:
    try:
        _trusted_networks.append(ipaddress.ip_network(entry, strict=False))
    except ValueError:
        continue


def _is_trusted(remote_addr):
    if not remote_addr or not _trusted_networks:
        return False
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(ip in net for net in _trusted_networks)


def get_client_ip(request):
    """Return the client IP, honouring proxy headers only when REMOTE_ADDR
    is in TRUSTED_PROXIES. Falls back to REMOTE_ADDR otherwise.
    """
    remote_addr = request.META.get('REMOTE_ADDR', '')
    if not _is_trusted(remote_addr):
        return remote_addr

    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded_for:
        # Leftmost is the client.
        candidate = forwarded_for.split(',')[0].strip()
        if candidate:
            return candidate

    real_ip = request.META.get('HTTP_X_REAL_IP')
    if real_ip:
        return real_ip.strip()

    return remote_addr
