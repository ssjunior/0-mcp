"""Translate an MCP tool call into a synthetic Django ``HttpRequest`` and
run it through the same middleware chain + ``BaseResource.dispatch`` as
REST.

No HTTP loopback. The synthetic request is wrapped with the project's
configured ``settings.MIDDLEWARE`` (Security, Auth, Exception, ...) so
rate limiting, IP blocking and session resolution behave exactly like a
direct REST hit.
"""
import json
import re
from urllib.parse import urlencode

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.test.client import RequestFactory
from django.utils.module_loading import import_string


_VERB_TO_METHOD = {
    'list':   'GET',
    'get':    'GET',
    'create': 'POST',
    'update': 'PATCH',
    'delete': 'DELETE',
}


def _route_pattern_to_path(pattern):
    """Convert ``^spaces(.*)$`` → ``/spaces``."""
    p = pattern.lstrip('^').rstrip('$')
    p = re.sub(r'\(\.\*\)$', '', p)
    if not p.startswith('/'):
        p = '/' + p
    return p.rstrip('/') or '/'


def _build_request(method, path, headers=None, body=None, query=None, envelope=None):
    """Build a synthetic Django HttpRequest carrying the given args.

    ``envelope`` carries the outer request's client identity (REMOTE_ADDR,
    X-Forwarded-For, X-Real-IP, User-Agent) so rate limiting and
    SecurityMiddleware see the real caller — without it, RequestFactory
    falls back to ``127.0.0.1`` and loopback exemptions silently bypass
    abuse controls on MCP traffic.
    """
    factory = RequestFactory()
    if query:
        path = f'{path}?{urlencode(query, doseq=True)}'

    body_bytes = b''
    content_type = 'application/json'
    if body is not None:
        body_bytes = json.dumps(body, default=str).encode('utf-8')

    # Pre-build environ so HTTP_HOST and friends are present BEFORE
    # ``request.headers`` (a cached snapshot of META) is built.
    extra = {}
    cookie_value = None
    if headers:
        for k, v in headers.items():
            if v is None:
                continue
            if k.lower() == 'cookie':
                cookie_value = v
            else:
                extra['HTTP_' + k.upper().replace('-', '_')] = v
    if envelope:
        if envelope.get('remote_addr'):
            extra['REMOTE_ADDR'] = envelope['remote_addr']
        if envelope.get('forwarded_for'):
            extra['HTTP_X_FORWARDED_FOR'] = envelope['forwarded_for']
        if envelope.get('real_ip'):
            extra['HTTP_X_REAL_IP'] = envelope['real_ip']
        if envelope.get('user_agent'):
            extra['HTTP_USER_AGENT'] = envelope['user_agent']
        if envelope.get('host'):
            extra['HTTP_HOST'] = envelope['host']
        if envelope.get('referer'):
            extra['HTTP_REFERER'] = envelope['referer']

    request = factory.generic(
        method.upper(), path, data=body_bytes, content_type=content_type,
        **extra,
    )
    # Marker so ``BaseResource._dispatch`` can apply MCP-only overrides
    # (mcp_fields, mcp_fk_expand) without affecting REST traffic.
    request.META['zeromcp.mcp_dispatch'] = True
    if cookie_value:
        request.META['HTTP_COOKIE'] = cookie_value
        for piece in cookie_value.split(';'):
            piece = piece.strip()
            if '=' in piece:
                ck, cv = piece.split('=', 1)
                request.COOKIES[ck.strip()] = cv.strip()
    return request


def _payload_from_response(response):
    if isinstance(response, JsonResponse):
        return json.loads(response.content.decode('utf-8'))
    if isinstance(response, HttpResponse):
        try:
            return json.loads(response.content.decode('utf-8'))
        except Exception:
            return {'_raw': response.content.decode('utf-8', errors='replace')}
    if isinstance(response, (dict, list)):
        return response
    return {'_raw': str(response)}


_NULL_LIKE = (None, '', '0.00', '0.0', '0.000')

# Same baseline as ``zeromcp.mcp.tools._DEFAULT_SENSITIVE`` — kept in
# both files to avoid an import cycle. Update both when extending.
_SENSITIVE_KEYS = frozenset({
    'password', 'password_hash', 'pwd',
    'api_key', 'api_secret',
    'secret', 'secret_key',
    'token', 'access_token', 'refresh_token',
    'private_key', 'session_key',
    'otp', 'otp_secret', 'two_factor_secret',
})


_MASK = '*********'


def _scrub_sensitive(payload, extra_keys):
    """Mask sensitive values in MCP response payloads.

    Replaces the value with ``'*********'`` instead of dropping the
    key — the agent still sees that the field exists (and what its
    name is) but never the actual contents. Symmetrical with REST,
    where ``BaseResource`` masks the same fields the same way."""
    blocked = _SENSITIVE_KEYS | {k.lower() for k in (extra_keys or [])}
    if isinstance(payload, dict):
        return {
            k: (_MASK if k.lower() in blocked else _scrub_sensitive(v, extra_keys))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_scrub_sensitive(v, extra_keys) for v in payload]
    return payload


def _is_null_like(v):
    """Values worth dropping from MCP responses to save tokens.

    ``None``, empty string, and the common decimal-zero string forms used
    by Django's ``DecimalField`` serializer. Numeric ``0`` / ``False`` are
    intentionally kept — those carry semantic meaning.
    """
    if v in _NULL_LIKE:
        return True
    if isinstance(v, (list, dict)) and not v:
        return True
    return False


def _omit_null(payload):
    """Recursively drop null-like fields. Lists keep position; dicts
    drop keys. Top-level keys (`meta`, `objects`, `_raw`, etc.) stay so
    the envelope shape is predictable."""
    if isinstance(payload, dict):
        return {
            k: _omit_null(v) for k, v in payload.items()
            if not _is_null_like(v)
        }
    if isinstance(payload, list):
        return [_omit_null(v) for v in payload]
    return payload


def _truncate(payload, max_text=2000, max_list=50):
    """Cap list lengths and string fields so the agent doesn't choke
    on huge responses. Applied only on the MCP layer — REST clients see
    the full response."""
    if isinstance(payload, dict):
        if 'objects' in payload and isinstance(payload['objects'], list):
            payload = dict(payload)
            full = len(payload['objects'])
            if full > max_list:
                payload['objects'] = payload['objects'][:max_list]
                payload.setdefault('meta', {})
                if isinstance(payload['meta'], dict):
                    payload['meta']['truncated'] = True
                    payload['meta']['returned'] = max_list
                    payload['meta']['total_in_page'] = full
        return {k: _truncate(v, max_text, max_list) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_truncate(v, max_text, max_list) for v in payload[:max_list]]
    if isinstance(payload, str) and len(payload) > max_text:
        return payload[:max_text] + '… (truncated)'
    return payload


async def call_tool(tool, args, ctx):
    """Run one tool through the resource's dispatch."""
    meta = tool.get('mcp_internal') or {}
    view_cls = meta.get('view_cls')
    kind = meta.get('kind')
    if not view_cls or not kind:
        raise RuntimeError('Tool missing internal metadata; rebuild the registry.')

    args = dict(args or {})
    headers = {
        'X-Api-Key': ctx.get('api_key'),
        'Cookie': ctx.get('cookie'),
    }

    base = _route_pattern_to_path(meta.get('route_pattern', ''))

    if kind == 'custom':
        route = meta['route']
        method = (route.get('allowed_methods') or ['get'])[0].upper()
        suffix = route['path'].lstrip('/').rstrip('$')
        # named groups in regex like (?P<id>...) become path params from args
        suffix = re.sub(
            r'\(\?P<([^>]+)>[^)]+\)',
            lambda m: str(args.pop(m.group(1), '')),
            suffix,
        )
        suffix = re.sub(r'\([^)]*\)', '', suffix)
        path = (base + '/' + suffix).rstrip('/') or '/'
        body = args if method in ('POST', 'PATCH') else None
        query = args if method == 'GET' else None
    else:
        method = _VERB_TO_METHOD[kind]
        if kind in ('get', 'update', 'delete'):
            rid = args.pop('id', None)
            path = f'{base}/{rid}'
        else:
            path = base

        body = None
        query = None
        if kind == 'create':
            body = args
        elif kind == 'update':
            body = args
        elif kind == 'list':
            query = args

    envelope = {
        'remote_addr': ctx.get('remote_addr'),
        'forwarded_for': ctx.get('forwarded_for'),
        'real_ip': ctx.get('real_ip'),
        'user_agent': ctx.get('user_agent'),
        'host': ctx.get('host'),
        'referer': ctx.get('referer'),
    }
    request = _build_request(
        method, path, headers=headers, body=body, query=query, envelope=envelope,
    )
    handler = _build_middleware_chain(view_cls)
    response = await handler(request)
    payload = _payload_from_response(response)
    extra_keys = list(getattr(view_cls, 'sensitive_fields', None) or [])
    extra_keys += list(getattr(view_cls, 'mcp_exclude_fields', None) or [])
    payload = _scrub_sensitive(payload, extra_keys)
    # ``list`` uses ``mcp_list_omit_null``; every other verb (get, create,
    # update, custom) uses ``mcp_edit_omit_null``. Both default to False so
    # the response shape stays consistent with the declared outputSchema —
    # set them on a resource only when token savings matter more than full
    # field visibility.
    if kind == 'list':
        omit = getattr(view_cls, 'mcp_list_omit_null', False)
    else:
        omit = getattr(view_cls, 'mcp_edit_omit_null', False)
    if omit:
        payload = _omit_null(payload)
    return _truncate(payload)


_chain_cache = {}


def _build_middleware_chain(view_cls):
    """Wrap ``view_cls.as_view()`` with project middleware (async chain).

    Cached by the resource class — ``as_view()`` returns a fresh callable
    every call, so caching by the callable would be a no-op. Only async-
    capable middleware is applied; sync-only middleware is skipped (REST↔MCP
    will diverge for sync-only middleware — see mcp-runtime docs).
    """
    cache_key = id(view_cls)
    cached = _chain_cache.get(cache_key)
    if cached is not None:
        return cached

    view = view_cls.as_view()
    handler = view
    if not iscoroutinefunction(handler):
        async def _async_view(request):
            return await view(request)
        handler = _async_view

    for mw_path in reversed(getattr(settings, 'MIDDLEWARE', [])):
        try:
            MWClass = import_string(mw_path)
        except ImportError:
            continue
        if not getattr(MWClass, 'async_capable', False):
            continue
        handler = MWClass(handler)

    _chain_cache[cache_key] = handler
    return handler
