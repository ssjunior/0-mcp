"""Pre-emptive security middleware.

Blocks IPs immediately on:
- Known scanner paths (`.env`, `wp-admin`, ...) and classic injection signatures
  in the path/querystring.
- Known scanner User-Agents (sqlmap, nikto, ...).
- Repeated 4xx responses within a short window (slow brute-force / probing).

All blocks share the same Redis key namespace as `BaseResource.block`, so a
single block fences both rate-limited abuse and signature-detected attacks.
"""
import re

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import JsonResponse

from .blocking import block as block_identifier
from .blocking import block_key as _block_key
from .blocking import is_blocked
from .client_ip import get_client_ip
from .redis_config import KEY_PREFIX, get_redis

from .settings_helper import get_setting

BLOCK_PATTERNS = get_setting('BLOCK_PATTERNS')
BLOCK_USER_AGENTS = get_setting('BLOCK_USER_AGENTS')
MAX_4XX_PER_MINUTE = get_setting('MAX_4XX_PER_MINUTE', default=10)
MAX_4XX_PER_HOUR = get_setting('MAX_4XX_PER_HOUR', default=30)
BLOCK_DURATION_SECONDS = get_setting('BLOCK_DURATION_SECONDS', default=86400)


DEFAULT_BLOCK_PATTERNS = [
    r'/\.(env|git|aws|ssh|svn|htaccess|htpasswd|DS_Store)',
    r'/(wp-admin|wp-login|wp-content|wp-includes|xmlrpc\.php)',
    r'/(phpmyadmin|pma|adminer|mysql|sqlite)',
    r'/(cgi-bin|fcgi-bin|server-status|server-info)',
    r'/(actuator|jmx-console|config\.json|appsettings\.json)',
    r'\.\./|%2e%2e|\.\.\\',
    r'\bunion\s+select\b|\bor\s+1\s*=\s*1\b|\bsleep\s*\(',
    r'<script\b|javascript:|onerror\s*=',
    r'/etc/passwd|/etc/shadow|/proc/self',
    r'\bphp://|\bdata:.*base64',
]

DEFAULT_BLOCK_USER_AGENTS = [
    'sqlmap', 'nikto', 'nmap', 'masscan', 'gobuster', 'dirbuster',
    'dirb', 'nuclei', 'wfuzz', 'ffuf', 'acunetix', 'nessus',
    'openvas', 'whatweb', 'wpscan', 'xsstrike', 'zgrab', 'shodan',
]


_pattern_re = re.compile(
    '|'.join(BLOCK_PATTERNS or DEFAULT_BLOCK_PATTERNS),
    re.IGNORECASE,
)
_ua_signatures = [s.lower() for s in (BLOCK_USER_AGENTS or DEFAULT_BLOCK_USER_AGENTS)]


def _4xx_key(ip):
    return f'{KEY_PREFIX}security:4xx:{ip}'


def _4xx_hour_key(ip):
    return f'{KEY_PREFIX}security:4xx_hour:{ip}'


async def _block(redis, ip, reason):
    await block_identifier(redis, ip, reason, ttl=BLOCK_DURATION_SECONDS)


async def _is_blocked(redis, ip):
    return await is_blocked(redis, ip)


def _matches_path(request):
    target = request.path
    qs = request.META.get('QUERY_STRING', '')
    if qs:
        target = f'{target}?{qs}'
    return _pattern_re.search(target)


def _matches_ua(request):
    ua = (request.META.get('HTTP_USER_AGENT') or '').lower()
    if not ua:
        return False
    return any(sig in ua for sig in _ua_signatures)


def _blocked_response():
    return JsonResponse(
        {'success': False, 'status': 403, 'detail': 'Blocked due to misbehavior'},
        status=403,
    )


class SecurityMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        # ``DEBUG = True`` projects skip every block path entirely —
        # local dev cycles legitimately produce the kind of traffic
        # patterns that would otherwise look abusive (404 sprees while
        # wiring up routes, repeated 401s while testing auth).
        from django.conf import settings as _django_settings
        if getattr(_django_settings, 'DEBUG', False):
            return await self.get_response(request)

        ip = get_client_ip(request)
        redis = get_redis()

        if ip and await _is_blocked(redis, ip):
            return _blocked_response()

        if _matches_path(request):
            if ip:
                await _block(redis, ip, 'pattern')
            return _blocked_response()

        if _matches_ua(request):
            if ip:
                await _block(redis, ip, 'ua')
            return _blocked_response()

        response = await self.get_response(request)

        if ip and 400 <= response.status_code < 500 and response.status_code != 429:
            minute_count = await redis.incr(_4xx_key(ip))
            if minute_count == 1:
                await redis.expire(_4xx_key(ip), 60)
            if minute_count > MAX_4XX_PER_MINUTE:
                await _block(redis, ip, '4xx-burst')
                return _blocked_response()

            hour_count = await redis.incr(_4xx_hour_key(ip))
            if hour_count == 1:
                await redis.expire(_4xx_hour_key(ip), 3600)
            if hour_count > MAX_4XX_PER_HOUR:
                await _block(redis, ip, '4xx-probe')
                return _blocked_response()

        return response
