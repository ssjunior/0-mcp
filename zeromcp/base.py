import hashlib
import json
import logging
from functools import reduce
from importlib import import_module
from urllib import parse
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.conf import settings as django_settings
from django.db import connections, models
from django.db.models import Q
from django.forms.models import model_to_dict
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.db.utils import IntegrityError
from django.utils import timezone

import operator
import re

from .auth import validate_token_async
from .blocking import block as block_identifier
from .blocking import is_blocked
from .client_ip import get_client_ip
from .filters import Filter as OrmFilter, validate_conditions
from .exception import HTTPException
from .helpers import (
    LOCAL_HOST,
    get_edit_related,
    get_related_objects,
    get_tz,
    method_not_allowed,
    re_id,
    save_through_model,
    search_regex,
)
from .schemas import is_pydantic_model, serialize_output, validate_input
from .serializer import CustomJSONEncoder
from .util import validate_session_key
from .rate_limit import RateLimiter
from .tenant.tenant import (
    apply_session_to_request,
    aset_tenant,
    get_api_session,
)
from .redis_config import KEY_PREFIX, get_redis
from .settings_helper import (
    get_cookie_id,
    get_read_only,
    get_session_ttl,
    get_setting,
    get_token_max_drift_ms,
)

COOKIE_ID = get_cookie_id()
SESSION_TTL = get_session_ttl()
TOKEN_MAX_DRIFT_MS = get_token_max_drift_ms()
ALLOWED_ORIGINS = get_setting('ALLOWED_ORIGINS', default=[])
ENFORCE_TOKEN = get_setting('ENFORCE_TOKEN', default=False)
AUTO_SCOPE_CACHE_BY_ACCOUNT = get_setting(
    'AUTO_SCOPE_CACHE_BY_ACCOUNT', default=True,
)
CACHE_TTL = get_setting('CACHE_TTL')
CACHE_TTL_ENABLE = get_setting('CACHE_TTL_ENABLE', default=True)
MCP_LIST_OMIT_NULL = get_setting('MCP_LIST_OMIT_NULL', default=False)
MCP_EDIT_OMIT_NULL = get_setting('MCP_EDIT_OMIT_NULL', default=False)
DEFAULT_AUTHENTICATED = get_setting('DEFAULT_AUTHENTICATED', default=True)
RATE_LIMITS = get_setting('RATE_LIMITS', default={
    'api': [
        {'interval': 1000, 'limit': 4},
        {'interval': 5000, 'limit': 20},
    ],
    'login': [
        {'interval': 5000, 'limit': 3},
        {'interval': 3600000, 'limit': 50},
    ],
    'abuse': [
        {'interval': 5000, 'limit': 20},
        {'interval': 3600000, 'limit': 200},
    ],
})


logger = logging.getLogger(__name__)
_WARNED_UNSCOPED_AUTH_CACHE = set()


def _warn_csrf_unprotected_at_startup():
    """Emit the anti-CSRF startup warning when auth is enabled
    globally but neither ``ENFORCE_TOKEN`` nor ``ALLOWED_ORIGINS`` is
    configured. Demo projects (``DEFAULT_AUTHENTICATED=False``) skip
    the warning — there's no cookie auth in play to harden.
    """
    if not DEFAULT_AUTHENTICATED:
        return
    if ENFORCE_TOKEN or ALLOWED_ORIGINS:
        return
    logger.warning(
        'authenticated mode is on but neither ENFORCE_TOKEN nor '
        'ALLOWED_ORIGINS is configured — no anti-CSRF defense at the '
        'framework level. Set at least one in the MCP settings dict, '
        'or harden cookies at the deployment layer.',
    )


_warn_csrf_unprotected_at_startup()


class BaseResource(View):
    # ``True`` historically. Override globally via
    # ``MCP = {'DEFAULT_AUTHENTICATED': False}`` (the generator emits
    # this for demo-friendly first-run experience), or per-resource
    # by setting ``authenticated = False`` on a subclass.
    authenticated = DEFAULT_AUTHENTICATED
    allowed_methods = ['delete', 'get', 'patch', 'post']
    routes = None

    # OpenAPI / MCP metadata. `summary` becomes the tag summary; `description`
    # becomes the long-form description in the generated spec.
    summary = None
    description = None

    # MCP exposure. None/True = expose every CRUD verb in `allowed_methods`.
    # False = hide entirely from the MCP server. List/tuple = explicit
    # whitelist of verbs (e.g. `['list', 'get']` for read-only).
    mcp_expose = None

    # MCP responses preserve null/empty fields by default (consistent
    # with REST and with the outputSchema contract — the agent always
    # sees the same shape). Opt in per-verb to compact responses where
    # token savings matter:
    #
    # - ``mcp_list_omit_null`` — drop null/empty/zero-decimal fields
    #   from ``list`` responses. Useful when listing many rows.
    # - ``mcp_edit_omit_null`` — same, applied to ``get``, ``create``,
    #   ``update`` and custom routes (single-record paths).
    #
    # Resolution order: framework default (``False``) → project setting
    # (``MCP = {'MCP_LIST_OMIT_NULL': True, ...}``) → resource
    # attribute (``mcp_list_omit_null = True`` on a subclass).
    #
    # The ``outputSchema`` of every tool still declares the full set of
    # fields, so the agent knows what exists either way.
    mcp_list_omit_null = MCP_LIST_OMIT_NULL
    mcp_edit_omit_null = MCP_EDIT_OMIT_NULL

    # Optional override: list of field names exposed via MCP tools. When
    # set, replaces ``list_fields`` / ``edit_fields`` for the MCP path
    # only — REST keeps using its own field lists. Useful for hiding
    # sensitive columns from agents while keeping them in the REST UI.
    mcp_fields = None

    # Optional FK expansion specific to MCP. Same shape as
    # ``list_related_fields``: ``{'currency': ['id', 'code'], ...}``.
    # When set, augments (not replaces) ``list_related_fields`` for MCP
    # list/get calls — REST is unaffected.
    mcp_fk_expand = None

    # Field names that should never leave the server, on **either**
    # surface — REST or MCP. Listed names are appended to
    # ``list_exclude_fields`` and ``edit_exclude_fields`` at init
    # time, and the MCP layer also strips them from tool schemas and
    # response payloads. The framework already filters a baseline of
    # obviously sensitive names (``password``, ``api_key``, ``token``,
    # ``otp``, ``secret``, …) — use this for project-specific columns
    # (audit fields, internal flags, server-assigned IDs).
    sensitive_fields = None
    # Backwards-compat alias — older code that set
    # ``mcp_exclude_fields`` still works. Prefer ``sensitive_fields``.
    mcp_exclude_fields = None

    account_db = 'default'
    cache = False
    # Default TTL: settings.CACHE_TTL when set, else 120s.
    # Subclasses can still override via ``cache_ttl = N`` and that wins.
    cache_ttl = CACHE_TTL if CACHE_TTL is not None else 120
    cache_namespace = None
    route_cache = False
    session_cache = False
    cache_scope_fields = None

    limit = 25
    page = 1
    order_by = 'id'
    count_results = False

    id = None
    model = None
    queryset = None
    tz = 'UTC'

    app_label = None
    model_name = None
    contextId = None

    fields = None
    all_fields = None
    fk_fields = None
    m2m_fields = None

    related_models = None
    list_related_fields = None
    many_to_many_models = None
    edit_prefetch_related = None
    list_prefetch_related = None

    filter_fields = None
    queryset_filter = None

    # Querystring param that resolves to a server-side stored filter
    # expression (e.g. ``'segment_id'``, ``'view_id'``). When set, the
    # framework reads ``request.GET[stored_filter_param]`` and calls
    # ``resolve_stored_filter(value)`` to load the conditions dict.
    # Combines with ``?filter=<json>`` via AND when both are present.
    stored_filter_param = None

    # When set (e.g. owner_field='owner_id'), every CRUD operation
    # (GET/LIST/PATCH/DELETE) is restricted to rows whose value of this
    # field matches the authenticated user's id. Default None disables
    # the check (consumer is responsible for ownership via
    # queryset_filter or custom get_obj/get_objs/delete_obj).
    owner_field = None

    # Default-deny owner spoofing on create: when the model has an
    # ``owner`` field, the framework forces ``owner_id`` to the
    # authenticated user and ignores any value sent by the client.
    # Set to True only on resources where an admin/integrator path
    # legitimately needs to create rows on behalf of another user.
    allow_owner_override = False
    search_fields = None
    order_fields = None
    list_fields = None
    list_exclude_fields = None
    edit_fields = None
    edit_related_fields = None
    edit_exclude_fields = None
    update_fields = None
    create_fields = None
    filters = None

    # Optional Pydantic schemas. When set, drive request validation,
    # response shaping and OpenAPI generation. Falls back to *_fields
    # when None.
    create_schema = None
    update_schema = None
    list_schema = None

    default_filter = None
    search_operator = 'icontains'

    obj = None
    obj_id = None
    data = None

    normalize_list = False
    normalize_obj = False
    normalized = False

    diff = None

    user = None
    account = None
    body = None

    def __init__(self):

        self.fields = list(self.fields) if self.fields else []
        self.all_fields = list(self.all_fields) if self.all_fields else []
        self.fk_fields = list(self.fk_fields) if self.fk_fields else []
        self.m2m_fields = list(self.m2m_fields) if self.m2m_fields else []
        self.related_models = dict(self.related_models) if self.related_models else {}
        self.list_related_fields = dict(self.list_related_fields) if self.list_related_fields else {}
        self.many_to_many_models = dict(self.many_to_many_models) if self.many_to_many_models else {}
        self.filter_fields = list(self.filter_fields) if self.filter_fields else []
        self.queryset_filter = dict(self.queryset_filter) if self.queryset_filter else {}
        self.search_fields = list(self.search_fields) if self.search_fields else []
        self.order_fields = list(self.order_fields) if self.order_fields else []
        self.list_fields = list(self.list_fields) if self.list_fields else []
        self.list_exclude_fields = list(self.list_exclude_fields) if self.list_exclude_fields else []
        self.edit_fields = list(self.edit_fields) if self.edit_fields else []
        self.edit_related_fields = dict(self.edit_related_fields) if self.edit_related_fields else {}
        self.edit_exclude_fields = list(self.edit_exclude_fields) if self.edit_exclude_fields else ['_state']
        # Normalise once so downstream serialisation can use a set
        # without recomputing per-row.
        self.sensitive_fields = set(self.sensitive_fields) if self.sensitive_fields else set()
        self.update_fields = list(self.update_fields) if self.update_fields else []
        self.create_fields = list(self.create_fields) if self.create_fields else []
        self.routes = list(self.routes) if self.routes else []
        self.filters = list(self.filters) if self.filters else []
        self.cache_scope_fields = self._normalize_cache_scope_fields(
            self.cache_scope_fields
        )
        self.diff = {}

        if self.model:
            fields = []
            for field in self.model._meta.get_fields():
                if field.is_relation and field.concrete and field.many_to_many:
                    self.m2m_fields.append(field.name)
                    continue

            for field in self.model._meta.local_fields:
                # ForeignKey/OneToOne: expose the underlying ``*_id`` column
                # (``created_by_id``, ``owner_id``) so the default LIST shape
                # round-trips FK references without an explicit ``list_fields``.
                fields.append(field.attname if field.is_relation else field.name)

            self.all_fields = list(fields) + self.m2m_fields

            self.fields = fields
            self.list_fields = self.list_fields or fields

            if not self.edit_fields:
                self.edit_fields = ['custom'] + [field.column for field in self.model._meta.local_fields]  # + m2m_fields

            self.queryset = self.model.objects

    def get_method(self, request, args, kwargs):
        self.request = request
        for route in self.routes:
            match = re.search(route['path'], request.path)
            if match:
                allowed_methods = route.get('allowed_methods')
                self.route_cache = route.get('cache', False)
                return getattr(self, route['func']), match.groupdict(), allowed_methods

        return None, None, None

    def get_allowed_domain(self, request):
        """Origin allowlist enforcement (anti-CSRF for cookie auth).

        Off when ``ALLOWED_ORIGINS`` is empty. Localhost passes
        unconditionally (for development). For everything else, the
        request's ``Origin`` header is matched against the allowlist
        (suffix match); ``Referer`` is consulted as fallback for
        same-origin GETs that omit ``Origin``. Mismatch → 403.
        """
        if not ALLOWED_ORIGINS:
            return

        host = request.headers.get('Host') or ''
        if LOCAL_HOST.match(host):
            return

        # Prefer ``Origin`` (set by browsers on cross-site requests and
        # not strippable by a malicious page). Fall back to ``Referer``
        # for older clients / same-origin GETs that may omit Origin.
        candidate = (
            request.headers.get('Origin')
            or request.headers.get('Referer')
        )
        if candidate:
            domain = urlparse(candidate).netloc.split(':')[0]
            for origin in ALLOWED_ORIGINS:
                if domain.endswith(origin):
                    return

        raise HTTPException(403, 'Not allowed')

    async def block(self, identifier):
        logger.warning('Blocking %s', identifier)
        redis = get_redis()
        await block_identifier(redis, identifier, 'api_rate_limit', ttl=86400)
        raise HTTPException(403, 'Blocked due to misbehavior')

    async def check_is_blocked(self, identifier):
        redis = get_redis()
        if await is_blocked(redis, identifier):
            raise HTTPException(403, 'Blocked due to misbehavior')

    async def _enforce_rate_limit(self, request):
        # Local development with ``DEBUG = True`` bypasses rate limiting
        # and abuse blocking. Hammering ``/docs`` while wiring up auth
        # would otherwise trigger a 24h block on 127.0.0.1 — annoying
        # and almost never the right behaviour for a dev box.
        if getattr(django_settings, 'DEBUG', False):
            return

        await self.check_is_blocked(self.identifier)

        if request.path.startswith('/login'):
            result = await RateLimiter.login_limited(self.identifier, RATE_LIMITS)
            if result['rate_limited']:
                await self.block(self.identifier)
            return

        result = await RateLimiter.api_limited(self.identifier, RATE_LIMITS)
        if result['abuse']:
            await self.block(self.identifier)
        if result['rate_limited']:
            raise HTTPException(429, 'Slow down, too many requests. You will be blocked.')

    async def _authenticate(self, request):
        """Resolve user/account from API key or session cookie. Returns
        the loaded session dict (or None) and the validated session key
        string (used by ``session_cache``; only returned when a real
        session was loaded — never for an unmatched cookie).

        Both paths populate the same ``request.*`` shape via the shared
        :func:`apply_session_to_request` helper, so downstream code sees
        identical attributes regardless of auth method."""
        if request.headers.get('X-Api-Key'):
            session = await get_api_session(request.headers.get('X-Api-Key'))
            if session:
                self._activate_session(session)
                await apply_session_to_request(request, session)
            return session, None

        session_key = validate_session_key(request.COOKIES.get(COOKIE_ID))
        if not session_key:
            return None, None

        existing = getattr(request, 'session', None)
        if existing:
            self._activate_session(existing)
            return existing, session_key

        redis = get_redis()
        raw = await redis.getex(
            f'{KEY_PREFIX}sessions:{session_key}', ex=SESSION_TTL,
        )
        if not raw:
            return None, None

        session = json.loads(raw)
        self._activate_session(session)
        await apply_session_to_request(request, session)
        return session, session_key

    def _activate_session(self, session):
        self.user = session['user']
        self.tz = get_tz(self.user.get('timezone', 'UTC'))
        timezone.activate(self.tz)
        self.account = session.get('account')

    async def _enforce_token(self, request):
        if not (self.authenticated and ENFORCE_TOKEN):
            return
        if request.path in ['/login', '/user/me']:
            return
        await validate_token_async(
            request.headers.get('X-Token'),
            self.user.get('token') if self.user else None,
            max_drift_ms=TOKEN_MAX_DRIFT_MS,
        )

    _CACHE_SCOPE_SOURCES = ('user', 'account')

    @classmethod
    def _normalize_cache_scope_fields(cls, raw):
        """Validate and normalize ``cache_scope_fields`` to a list of
        ``(source, field)`` tuples. Strings are shorthand for ``user``."""
        if not raw:
            return []
        normalized = []
        for entry in raw:
            if isinstance(entry, str):
                normalized.append(('user', entry))
                continue
            if (
                isinstance(entry, tuple) and len(entry) == 2
                and isinstance(entry[0], str) and isinstance(entry[1], str)
            ):
                source, field = entry
                if source not in cls._CACHE_SCOPE_SOURCES:
                    raise ValueError(
                        f'Invalid cache_scope_fields source {source!r} on '
                        f'{cls.__name__}; expected one of '
                        f'{cls._CACHE_SCOPE_SOURCES}'
                    )
                normalized.append((source, field))
                continue
            raise ValueError(
                f'Invalid cache_scope_fields entry {entry!r} on '
                f'{cls.__name__}; expected str or (source, field) tuple'
            )
        return normalized

    def _resolve_cache_scope(self):
        """Build the scope hash for the cache key.

        Three outcomes:

        - ``None`` *with cache enabled*: no scope fields configured, or
          fully anonymous request. Cache continues normally; the key
          simply has no scope segment.
        - ``str`` segment (``'scope=...'``): fold succeeded.
        - ``None`` *with cache disabled*: scope was configured but the
          required field is missing from the session payload. The
          framework logs a ``WARNING`` and **disables cache for this
          request** (``self.cache = False``) so the response is neither
          read from nor written to Redis. Sharing a cache key across
          users when the scope can't be resolved would be a silent
          leak across whatever dimension the operator was protecting.
        """
        if not self.cache_scope_fields:
            return None

        # Fully anonymous request → nothing to scope by, skip silently.
        if self.user is None and self.account is None:
            return None

        sources = {'user': self.user, 'account': self.account}
        parts = []
        for source, field in self.cache_scope_fields:
            container = sources.get(source)
            if container is None:
                logger.warning(
                    "cache scope source %r missing from session for %s; "
                    "disabling cache for this request (populate the "
                    "session payload to enable caching)",
                    source, self.__class__.__name__,
                )
                self.cache = False
                return None
            if field not in container:
                logger.warning(
                    "cache scope field %r missing from %s session payload "
                    "for %s; disabling cache for this request (populate "
                    "the session payload to enable caching)",
                    field, source, self.__class__.__name__,
                )
                self.cache = False
                return None
            parts.append(f'{source}.{field}={container[field]!r}')

        digest = hashlib.md5(':'.join(parts).encode('utf-8')).hexdigest()[:16]
        return f'scope={digest}'

    def _account_cache_segment(self):
        """Segment that isolates the cache between tenants.

        Returns ``None`` when no account is active (anonymous endpoints,
        single-tenant deployments, or the auto-scope setting is off) so
        callers can skip the fold cleanly. Otherwise returns a segment
        like ``'a=42'`` to be appended to the cache key.

        Projects that override ``_build_cache_key`` should call this
        helper to inherit the automatic tenant isolation.
        """
        if not AUTO_SCOPE_CACHE_BY_ACCOUNT:
            return None
        if getattr(self, 'account_id', None) is None:
            return None
        return f'a={self.account_id}'

    def _warn_if_authenticated_cache_unscoped(self):
        """Warn once per resource class when authenticated cache lacks
        enough isolation for the current request context.

        Account auto-folding already isolates multi-tenant traffic when
        ``self.account_id`` is active, so that case should not nag app
        code to redundantly declare ``cache_scope_fields=[('account',
        'id')]``. We warn only when an authenticated cached request is
        effectively shared across users for the current runtime context:

        - no ``session_cache``
        - no explicit ``cache_scope_fields``
        - and either account auto-scope is disabled or no account is
          active for the request (single-tenant / anonymous-account path)
        """
        if not (
            self.cache
            and self.authenticated
            and self.cache_ttl
            and not self.session_cache
            and not self.cache_scope_fields
        ):
            return

        if AUTO_SCOPE_CACHE_BY_ACCOUNT and getattr(self, 'account_id', None) is not None:
            return

        warning_key = (
            self.__class__,
            bool(AUTO_SCOPE_CACHE_BY_ACCOUNT),
            getattr(self, 'account_id', None) is not None,
        )
        if warning_key in _WARNED_UNSCOPED_AUTH_CACHE:
            return
        _WARNED_UNSCOPED_AUTH_CACHE.add(warning_key)

        logger.warning(
            'Resource %s: cache=True on authenticated endpoint without '
            'session_cache or cache_scope_fields, and no active account '
            'auto-scope for this request. Cached response may leak '
            'across users. Set session_cache=True or '
            'cache_scope_fields=(...) to scope the cache key per-user.',
            self.__class__.__name__,
        )

    def _build_cache_key(self, request, session_key):
        key = f'{KEY_PREFIX}cache' if KEY_PREFIX else '0-mcp:cache'
        if self.session_cache and session_key:
            key += f':{session_key}'
        account_segment = self._account_cache_segment()
        if account_segment:
            key += f':{account_segment}'
        scope = self._resolve_cache_scope()
        if scope:
            key += f':{scope}'
        key += f':{request.path}'
        qs = request.GET.urlencode()
        if qs:
            key += f':{hashlib.md5(qs.encode()).hexdigest()}'
        return key

    def _build_cache_namespace(self, request, is_custom_route):
        label = self.model._meta.label_lower if self.model else 'unknown'
        if is_custom_route:
            user_id = self.user.get('id') if self.user else None
            return f'detail:{label}:{user_id}' if user_id is not None else f'list:{label}'

        match = re_id.match(request.path_info)
        if match:
            obj_id = match.group('uuid') or match.group('int_id')
            return f'detail:{label}:{obj_id}'
        return f'list:{label}'

    async def _serve_cache(self, request, session_key, is_custom_route):
        """Build cache key/namespace, run before_cache hook, account
        hit/miss stats, and return a cached HttpResponse on hit (or None
        on miss)."""
        self.cache_key = self._build_cache_key(request, session_key)
        self.cache_namespace = self._build_cache_namespace(request, is_custom_route)

        await self.before_cache(request)

        redis = get_redis()
        response = await redis.get(self.cache_key)

        label = self.model._meta.label_lower if self.model else 'unknown'
        outcome = 'hits' if response else 'misses'
        # Per-model counters live in a single hash so ``get_cache_stats``
        # can read them with one HGETALL instead of SCAN over the whole
        # keyspace (the original ``cache_stats:hits:<label>`` layout
        # cost 10+ seconds on production-sized Redis).
        from .redis_config import _BY_MODEL_KEY, cache_stats_field
        pipe = redis.pipeline()
        pipe.incr(f'{KEY_PREFIX}cache_stats:{outcome}')
        pipe.hincrby(_BY_MODEL_KEY, cache_stats_field(outcome, label), 1)
        await pipe.execute()

        if response:
            return HttpResponse(response, content_type='application/json')
        return None

    async def _parse_body(self, request):
        if self.method in ['get', 'patch']:
            match = re_id.match(request.path_info)
            if match:
                self.id = match.group('int_id') or match.group('uuid')

        if self.method in ['post', 'patch']:
            if request.content_type == 'application/json':
                try:
                    self.body = json.loads(request.body.decode('utf-8'))
                except Exception:
                    raise HTTPException(400, 'Invalid body')
                schema = self.create_schema if self.method == 'post' else self.update_schema
                if is_pydantic_model(schema):
                    self.body = validate_input(schema, self.body)
                await self.hydrate(self.body)
        else:
            self.body = None

    async def _run_handler(self, request, func, handler, match):
        try:
            if func:
                if self.method in ['post', 'patch']:
                    return await func(request, match=match, body=self.body)
                return await func(request, match=match)
            return await handler(request)
        except HTTPException:
            raise
        except Exception:
            logger.exception('Unhandled error in %s %s', self.method.upper(), request.path)
            if getattr(django_settings, 'DEBUG', False):
                raise
            return JsonResponse(
                {'success': False, 'status': 500, 'detail': 'Internal error'},
                status=500,
            )

    async def dispatch(self, request, *args, **kwargs) -> None:
        """Outer wrapper — guarantees unhandled exceptions never reach
        the wire silently. ``HTTPException`` re-raises (rendered by
        :class:`ExceptionMiddleware`); anything else gets logged with
        full traceback and returns a sanitized JSON 500.

        Without this net, exceptions from ``_authenticate``, ``aset_tenant``,
        ``_serve_cache``, ``pre_process``, ``_parse_body``, ``build_filters``
        or ``serialize`` bubbled to Django's default async handler with no
        0-mcp-side log — Sentry stayed empty and operators were blind.
        """
        try:
            return await self._dispatch(request, *args, **kwargs)
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                'Unhandled error in dispatch %s %s',
                request.method, request.path,
            )
            if getattr(django_settings, 'DEBUG', False):
                raise
            return JsonResponse(
                {'success': False, 'status': 500, 'detail': 'Internal error'},
                status=500,
            )

    async def _dispatch(self, request, *args, **kwargs) -> None:
        self.identifier = get_client_ip(request)

        # MCP-specific overrides — applied only when the request was built
        # by ``zeromcp.mcp.bridge`` (it sets the marker below). Keeps REST
        # paths untouched.
        if request.META.get('zeromcp.mcp_dispatch'):
            if self.mcp_fields:
                self.list_fields = list(self.mcp_fields)
            if self.mcp_fk_expand:
                self.list_related_fields = {
                    **(self.list_related_fields or {}),
                    **self.mcp_fk_expand,
                }

        await self._enforce_rate_limit(request)

        session, session_key = await self._authenticate(request)

        if self.authenticated and not session:
            raise HTTPException(401, 'Not authorized')

        await self._enforce_token(request)

        if ALLOWED_ORIGINS:
            self.get_allowed_domain(request)

        if session and session.get('account'):
            self.account_db = await aset_tenant(session['account']['id'])
            self.account_id = session['account']['id']

        self.method = 'get' if request.method == 'HEAD' else request.method.lower()

        # func is the method to execute when a custom route matches
        func, match, allowed_methods = self.get_method(request, args, kwargs)

        if func:
            self.allowed_methods = allowed_methods or self.allowed_methods

        if get_read_only() and self.method != 'get':
            raise HTTPException(405, f'{self.method.upper()} not allowed (READ_ONLY)')

        if self.method not in self.allowed_methods:
            raise HTTPException(405, f'{self.method.upper()} not allowed')

        handler = getattr(self, self.method, method_not_allowed) if not func else None

        is_custom_route = func is not None
        # ``CACHE_TTL_ENABLE = False`` is a global kill switch — treats
        # every ``cache=True`` resource as off without code edits.
        # Useful for incident response (cache poisoning, stale data).
        self.cache = (
            self.cache
            and CACHE_TTL_ENABLE
            and self.method == 'get'
            and (not is_custom_route or self.route_cache)
        )
        if self.cache:
            self._warn_if_authenticated_cache_unscoped()
            cached = await self._serve_cache(request, session_key, is_custom_route)
            if cached is not None:
                return cached

        await self.pre_process(request)
        await self._parse_body(request)

        if self.method == 'get' and not self.id:
            await self.build_filters(request)
            self.paginate(request)
            self.ordenate(request)

        if self.queryset_filter:
            self.queryset = self.queryset.filter(**self.queryset_filter)

        response = await self._run_handler(request, func, handler, match)

        if type(response) in [dict, list]:
            response = await self.serialize(response)
        elif self.cache and isinstance(response, JsonResponse):
            # Handler returned a built JsonResponse — serialize() would
            # short-circuit before save_cache, leaving the cache cold
            # forever. Persist the response body directly so the next
            # hit on this key is served by _serve_cache.
            await self._save_response_cache(response)

        return response

    async def build_filters(self, request):
        if hasattr(self, 'model_filter'):
            self.queryset = self.queryset.filter(**self.model_filter)

        if self.queryset is None:
            return

        if request.GET.get('search'):
            search_fields = self.search_fields + ['id']
            filters = reduce(
                operator.or_, [
                    Q((f'{field}__{self.search_operator}',
                      request.GET.get('search')))
                    for field in search_fields
                ]
            )
            self.queryset = self.queryset.filter(filters)

        params = dict(request.GET)
        if self.filter_fields:
            filter = {}
            for key in params:
                if key == 'normalize':
                    self.normalize_list = True
                    continue

                if search_regex.search(key):
                    keys = key.split('__')
                    if len(keys) == 3:
                        field = f'{keys[0]}__{keys[1]}'
                    else:
                        field = keys[0]

                else:
                    field = key

                if field in self.filter_fields:
                    param = params[key][0]
                    # Coerce ``true``/``false`` into bools only when the
                    # lookup actually expects a boolean — boolean model
                    # fields, or the ``__isnull`` lookup which Django
                    # rejects with a string.
                    coerce_bool = key.endswith('__isnull')
                    if not coerce_bool and self.model:
                        try:
                            model_field = self.model._meta.get_field(field)
                            coerce_bool = isinstance(model_field, models.BooleanField)
                        except Exception:
                            pass
                    if coerce_bool:
                        if param.lower() == 'false':
                            param = False
                        elif param.lower() == 'true':
                            param = True

                    filter[key] = param

            self.queryset = self.queryset.filter(**filter)

        if (
            self.model and f'{self.model._meta.app_label}_{self.model._meta.model_name}' == 'core_tag'
        ):
            context = request.GET.get('context')
            if context:
                self.queryset = self.queryset.filter(context=context)

        tags = request.GET.get('tags')
        if tags and hasattr(self.model, 'tags'):
            tags_operator = request.GET.get('tags_operator', 'OR')
            tags_ids = tags.split(',')
            if tags_operator == 'OR':
                self.queryset = self.queryset.filter(tags__id__in=tags_ids)

    def ordenate(self, request):
        order_by = request.GET.get('order_by')
        if order_by and order_by.split('-')[-1] in self.order_fields:
            self.order_by = order_by

    def paginate(self, request):
        page = request.GET.get('page')
        if page:
            try:
                self.page = int(page)
            except Exception:
                pass

        limit = request.GET.get('limit')
        if limit:
            try:
                self.limit = int(limit)
            except Exception:
                pass

    async def serialize(self, result, **kwargs):

        if type(result) is JsonResponse:
            return result

        response = kwargs.get('response')
        if response:
            return response

        if not self.count_results:

            if isinstance(result, list):
                for row in result:
                    await self.dehydrate(row)

            elif type(result) is dict and 'objects' in result:
                for row in result['objects']:
                    await self.dehydrate(row)

            else:
                await self.dehydrate(result)

            if is_pydantic_model(self.list_schema):
                if isinstance(result, list):
                    result = serialize_output(self.list_schema, result)
                elif type(result) is dict and 'objects' in result:
                    result['objects'] = serialize_output(self.list_schema, result['objects'])
                elif isinstance(result, dict):
                    result = serialize_output(self.list_schema, result)

        result = await self.post_process(result)
        await self.save_cache(result)

        return JsonResponse(result, encoder=CustomJSONEncoder.with_timezone(self.tz), safe=False)

    async def save_cache(self, content):
        if not self.cache:
            return

        redis = get_redis()
        await redis.setex(
            self.cache_key,
            self.cache_ttl,
            json.dumps(content, cls=CustomJSONEncoder.with_timezone(self.tz)),
        )
        if self.cache_namespace:
            ns_key = f'{KEY_PREFIX}cache_ns:{self.cache_namespace}'
            await redis.sadd(ns_key, self.cache_key)
            await redis.expire(ns_key, max(self.cache_ttl * 2, 86400))

    async def _save_response_cache(self, response):
        """Cache a pre-built JsonResponse's body.

        Mirrors :meth:`save_cache` but skips JSON re-encoding — the
        ``response.content`` is already JSON bytes. Used when a handler
        returns ``JsonResponse(...)`` directly (a common pattern for
        custom ``get_objs`` overrides) and ``serialize`` would otherwise
        short-circuit before ``save_cache`` runs.
        """
        if not self.cache:
            return
        body = response.content
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        redis = get_redis()
        await redis.setex(self.cache_key, self.cache_ttl, body)
        if self.cache_namespace:
            ns_key = f'{KEY_PREFIX}cache_ns:{self.cache_namespace}'
            await redis.sadd(ns_key, self.cache_key)
            await redis.expire(ns_key, max(self.cache_ttl * 2, 86400))

    async def invalidate_cache(self, namespaces):
        if not namespaces:
            return
        redis = get_redis()
        for ns in namespaces:
            ns_key = f'{KEY_PREFIX}cache_ns:{ns}'
            keys = await redis.smembers(ns_key)
            if keys:
                await redis.delete(*keys)
            await redis.delete(ns_key)

    def _cache_namespaces(self, include_detail_id=None):
        if not self.model:
            return []
        label = self.model._meta.label_lower
        ns = [f'list:{label}']
        if include_detail_id is not None:
            ns.append(f'detail:{label}:{include_detail_id}')
        return ns

    async def add_m2m(self, result):
        pass

    async def alter_detail(self, result):
        return result

    async def alter_list(self, results):
        return results

    async def hydrate(self, body):
        pass

    async def before_cache(self, request):
        pass

    async def pre_process(self, request):
        pass

    async def dehydrate(self, response):
        pass

    async def post_process(self, response):
        return response

    #########################################################
    # GET
    #########################################################

    @sync_to_async
    def count(self):
        # InnoDB count(*) and Django's queryset.count() are slow on large tables
        # because they add subquery wrapping with joins. We rewrite the SELECT
        # directly via regex to avoid the wrap while keeping the original WHERE/JOIN.
        count = 0
        self.count_results = 10
        if not hasattr(self.queryset, 'query'):
            self.queryset = self.queryset.all()

        query, params = self.queryset.query.sql_with_params()
        table = self.model._meta.db_table
        query = re.sub(
            r'^SELECT .*? FROM',
            f'SELECT count(DISTINCT {table}.id) FROM',
            query,
        )

        connection = connections[self.account_db]
        cursor = connection.cursor()
        cursor.execute(query, params)
        count = cursor.fetchone()[0]

        self.count_results = {'count': count}

    def _validate_filter_fields(self, conditions):
        validate_conditions(conditions, self.filter_fields)

    async def resolve_stored_filter(self, value, **kwargs):
        """Hook: turn a stored-filter id into a Layer-2 conditions dict.

        Override on resources that declare ``stored_filter_param``. The
        return value is trusted verbatim — apply tenant/RBAC scoping,
        soft-delete filters and any policy check inside the override.
        Return ``None`` to signal not-found (framework raises 404), or a
        conditions dict shaped like the ``?filter=<json>`` payload.
        """
        return None

    async def get_filters(self, request):
        if self.filters:
            self.queryset = self.queryset.filter(self.filters)

        filter_ = request.GET.get('filter')

        normalize_list = request.GET.get('normalize_list')
        if normalize_list:
            self.normalize_list = normalize_list.lower() == 'true'

        stored_value = (
            request.GET.get(self.stored_filter_param)
            if self.stored_filter_param else None
        )

        if filter_ is None and stored_value is None:
            return

        groups = []

        if stored_value is not None:
            stored_conditions = await self.resolve_stored_filter(stored_value)
            if stored_conditions is None:
                raise HTTPException(404, 'Stored filter not found')
            if stored_conditions:
                groups.append(stored_conditions)

        if filter_:
            url_conditions = json.loads(filter_)
            if url_conditions:
                # ``?filter=`` is caller-controlled — must pass the
                # whitelist. Stored filters are trusted because the hook
                # owns the lookup (and any validation, scoping, RBAC).
                self._validate_filter_fields(url_conditions)
                groups.append(url_conditions)

        if not groups:
            return

        if len(groups) == 1:
            conditions = groups[0]
        else:
            conditions = {'logical_operator': 'AND', 'rules': groups}

        orm_filter = OrmFilter(
            self.model,
            self.user.get('timezone', 'UTC') if self.user else 'UTC',
            base_queryset=self.queryset
        )
        queryset = orm_filter.filter_by(conditions)

        if request.GET.get('search'):
            search_fields = self.search_fields + ['id']
            filters = reduce(
                operator.or_, [
                    Q((f'{field}__{self.search_operator}',
                      request.GET.get('search')))
                    for field in search_fields
                ]
            )
            queryset = queryset.filter(filters)

        if self.filters:
            queryset = queryset.filter(self.filters)

        self.queryset = queryset.distinct()

    async def return_results(self, results):

        if type(results) is JsonResponse:
            return results

        if self.count_results:
            return self.count_results

        results = await self.alter_list(results)

        if self.normalized:
            return results

        if self.normalize_list and isinstance(results, list):
            normalized = {}
            for result in results:
                normalized[result['id']] = result
            return normalized

        result = {}
        if self.limit:
            # ``self.request.GET`` is a QueryDict — ``dict(...)`` collapses
            # multi-valued keys to their last value (good enough for paging
            # links). Without this, ``{**QueryDict}`` can leak list values
            # that ``urlencode`` then encodes as ``[%27...%27]`` literals.
            params = self.request.GET.dict() if self.request.GET else {}
            params['page'] = self.page + 1
            next_page = self.request.path + '?' + parse.urlencode(params)

            result['meta'] = {
                'page': self.page,
                'limit': self.limit,
                'next': next_page,
            }

            if self.page > 1:
                params['page'] = self.page - 1
                previous_page = self.request.path + \
                    '?' + parse.urlencode(params)
                result['meta']['previous'] = previous_page

        result['objects'] = results
        return result

    async def get_objs(self, request):
        await self.get_filters(request)

        ownership = self._ownership_filter()
        if ownership:
            self.queryset = self.queryset.filter(**ownership)

        if request.GET.get('count'):
            return await self.count()

        if self.page > 0:
            start = (self.page - 1) * self.limit
        else:
            start = 0

        if self.list_related_fields:
            self.queryset = self.queryset.select_related(*self.list_related_fields.keys())

        prefetch_fields = self.list_prefetch_related.keys() if self.list_prefetch_related else []
        if prefetch_fields:
            self.queryset = self.queryset.prefetch_related(*prefetch_fields)

        self.queryset = self.queryset.order_by(
            self.order_by
        )

        if self.limit:
            self.queryset = self.queryset[start:start + self.limit]

        results = []

        fields = request.GET.get('fields')
        if fields:
            requested = [f.strip() for f in fields.split(',') if f.strip()]
            allowed = set(self.list_fields or [])
            list_fields = [f for f in requested if f in allowed]
            related = False
        else:
            list_fields = self.list_fields
            related = True

        async for row in self.queryset:
            result = {}
            if related:
                for key, fields in self.list_related_fields.items():
                    model = key.split('__')
                    count = len(model) - 1
                    reduce(
                        get_related_objects, model, (row, result, count, key, self.related_models, self.list_related_fields)
                    )

            for field in list_fields:
                if field in self.list_exclude_fields:
                    continue

                if field == 'password' or field in self.sensitive_fields:
                    result[field] = '*********'
                else:
                    result[field] = getattr(row, field, None)

            for field in prefetch_fields:
                result[field] = []
                query = getattr(row, field)
                async for prefetch in query.values(*self.list_prefetch_related[field]):
                    result[field].append(prefetch)

            results.append(result)

        return results

    async def return_result(self, result):
        for key in list(result):
            if (
                self.edit_fields and
                key not in self.edit_fields and
                key not in self.edit_related_fields and
                key != '_result' and
                key != 'custom' and
                key not in self.related_models and
                key not in self.m2m_fields
            ):
                result.pop(key, None)

            if self.edit_exclude_fields and key in self.edit_exclude_fields:
                result.pop(key, None)

        result = await self.alter_detail(result)

        if self.normalize_obj:
            normalized = {}
            normalized[result['id']] = result
            return await self.serialize(normalized)

        return result

    async def get_obj(self, id):
        related_fields = []
        related = []
        m2m = []

        if self.filters:
            self.queryset = self.queryset.filter(self.filters)

        if self.edit_related_fields:
            for key in self.edit_related_fields.keys():
                if key in self.m2m_fields:
                    m2m.append(key)
                else:
                    # Add everything that is not M2M (direct and chained FKs)
                    related.append(key)

            for key in related:
                for rf in self.edit_related_fields[key]:
                    rf = rf.split('__')
                    if len(rf) > 1:
                        new_rf = key + '__' + '__'.join(rf[:-1])
                        related_fields.append(new_rf)
                    else:
                        related_fields.append(key)

            try:
                self.queryset = self.queryset.select_related(*related_fields)
            except Exception as e:
                raise HTTPException(500, f'Invalid related field in edit_related_fields: {str(e)}')

        prefetch_fields = self.edit_prefetch_related.keys() if self.edit_prefetch_related else []
        if prefetch_fields:
            self.queryset = self.queryset.prefetch_related(*prefetch_fields)

        self.obj = await self.queryset.filter(
            pk=id, **self._ownership_filter(),
        ).afirst()

        if not self.obj:
            # Same 404 whether the row doesn't exist or belongs to
            # another owner — never leak existence cross-owner.
            raise HTTPException(404, 'Object does not exist')

        result = {}

        for model in related:
            fields = self.edit_related_fields[model]

            final = {}
            obj = self.obj

            if hasattr(obj, model) and not getattr(obj, model):
                result[model] = None
                continue

            reduce(get_edit_related, fields, {'model': model, 'obj': obj, 'result': final})
            result[model] = final

        for related_field in m2m:
            fields = self.edit_related_fields[related_field]
            related_field = self.obj._meta.get_field(related_field)
            key = related_field.name
            query = getattr(self.obj, key)
            result[key] = []
            async for item in query.values(*fields):
                result[key].append(item)

        for field in self.edit_fields:
            if field == 'password' or field in self.sensitive_fields:
                result[field] = '*********'
            else:
                result[field] = getattr(self.obj, field, None)

        for field in prefetch_fields:
            result[field] = []
            query = getattr(self.obj, field)
            async for prefetch in query.values(*self.edit_prefetch_related[field]):
                result[field].append(prefetch)

        return result

    async def _get_objs(self, request):
        data = await self.get_objs(request)
        return await self.return_results(data)

    async def get(self, request):
        if self.id:
            data = await self.get_obj(self.id)
            data = await self.alter_detail(data)
            if self.normalize_obj:
                normalized = {}
                normalized[data['id']] = data
                return await self.serialize(normalized)

            return await self.serialize(data)
        else:
            data = await self._get_objs(request)
            return await self.serialize(data)

    #########################################################
    # DELETE
    #########################################################
    def _ownership_filter(self):
        """Return extra ``filter`` kwargs that scope queries to the
        authenticated user when ``owner_field`` is set. Empty dict when
        no ownership check is configured."""
        if not self.owner_field:
            return {}
        if not self.user or not self.user.get('id'):
            raise HTTPException(401, 'Not authorized')
        return {self.owner_field: self.user['id']}

    async def delete_obj(self, id):
        qs = self.queryset.filter(pk=id, **self._ownership_filter())

        if self.owner_field and not await qs.aexists():
            raise HTTPException(404, 'Item not found')

        try:
            await qs.adelete()
        except Exception:
            logger.exception(
                'delete failed for %s id=%s',
                self.model._meta.label if self.model else '<unknown>', id,
            )
            raise HTTPException(400, 'Could not delete item')

        return {'success': True, 'id': id, 'message': 'Deleted'}

    async def delete(self, request):
        id_match = re_id.match(request.path_info)
        if id_match:
            uuid = id_match.group('uuid')
            id = id_match.group('int_id')
            self.id = id or uuid
            results = await self.delete_obj(self.id)
            await self.invalidate_cache(self._cache_namespaces(include_detail_id=self.id))
            return await self.serialize(results)
        else:
            raise HTTPException(404, 'Item not found')

    #########################################################
    # PATCH
    #########################################################

    async def update_obj(self, id, body):
        keys = []
        for key in list(body.keys()):
            if key.startswith('custom_'):
                continue
            keys.append(key)

        allowed = False
        diff = None
        if self.update_fields:
            diff = list(set(keys) - set(self.update_fields))
            allowed = not diff
            diff = (', ').join(list(diff))

        if not allowed:
            if self.update_fields:
                raise HTTPException(403, f'Changes on field(s): {diff} is not allowed')
            raise HTTPException(500, 'Update fields not defined')

        try:
            self.obj = await self.queryset.aget(
                pk=id, **self._ownership_filter(),
            )
        except Exception:
            # Same 404 whether the row doesn't exist or belongs to
            # another owner — never leak existence cross-owner.
            raise HTTPException(404, 'Item not found')

        to_update = {}
        for key, value in body.items():

            if key in self.m2m_fields:
                query = getattr(self.obj, key)
                await query.aset(value)
                continue

            if key.startswith('custom_'):
                await self.obj._custom.aset(key.replace('custom_', ''), value)

            else:
                field = getattr(self.model, key)
                if field.field.primary_key:
                    key += '_id'
                    target = getattr(field.field, 'target_field', field.field)
                    if target.get_internal_type() in (
                        'AutoField', 'BigAutoField', 'SmallAutoField',
                        'IntegerField', 'BigIntegerField',
                        'PositiveIntegerField', 'PositiveBigIntegerField',
                        'PositiveSmallIntegerField', 'SmallIntegerField',
                    ):
                        value = int(value)

                if isinstance(field.field, models.ForeignKey):
                    if not key.endswith('_id'):
                        key += '_id'

                    if type(value) is dict:
                        value = value['id']

                to_update[key] = value

                old_value = getattr(self.obj, key)
                self.diff[key] = {'old': old_value, 'new': value}

                setattr(self.obj, key, value)

        if to_update:
            await self.model.objects.filter(
                pk=id, **self._ownership_filter(),
            ).aupdate(**to_update)

        return await self.get_obj(id)

    async def patch(self, request):
        result = await self.update_obj(self.id, self.body)
        result = await self.return_result(result)
        await self.invalidate_cache(self._cache_namespaces(include_detail_id=self.id))
        if self.normalize_obj:
            return result
        return await self.serialize(result)

    #########################################################
    # POST
    #########################################################
    async def create_obj(self, request, body):
        keys = []
        to_save = {}
        custom = {}
        for key in list(body.keys()):
            if key.startswith('custom_'):
                custom[key] = body[key]
            else:
                keys.append(key)

        allowed = False
        diff = None
        if self.create_fields:
            diff = list(set(keys) - set(self.create_fields))
            allowed = not diff
            diff = (', ').join(diff)

        if not allowed:
            if self.create_fields:
                raise HTTPException(403, f'Creation on field(s): {diff} is not allowed')
            raise HTTPException(500, 'Create fields not defined')

        user = self.user

        if user:
            if 'created_by_id' in self.all_fields:
                body['created_by_id'] = user['id']
            if 'updated_by_id' in self.all_fields:
                body['updated_by_id'] = user['id']
            if 'owner_id' in self.all_fields:
                if self.allow_owner_override:
                    body['owner_id'] = body.get('owner_id', user['id'])
                else:
                    body['owner_id'] = user['id']

        blank_errors = []
        null_errors = []

        for field in self.model._meta.local_fields:
            if field.primary_key:
                continue

            allow_blank = field.blank
            allow_null = field.null
            default = field.has_default() or hasattr(field, 'auto_now') or hasattr(field, 'auto_now_add')

            field_key = f'{field.name}_id' if field.is_relation else field.name

            field_value = body.get(field_key)

            if not default and not allow_blank and field_value == '':
                blank_errors.append(field.verbose_name)

            if not default and not allow_null and field_value is None:
                null_errors.append(field.verbose_name)

            if body.get(field_key) is not None:
                to_save[field_key] = body[field_key]

        if blank_errors or null_errors:
            errors = ''
            if blank_errors:
                errors += 'Field(s): ' + ', '.join(blank_errors) + ' can\'t be blank. '

            if null_errors:
                errors += 'Field(s): ' + ', '.join(null_errors) + ' can\'t be null.'

            raise HTTPException(403, errors)

        try:
            obj = await self.model.objects.acreate(**to_save)

        except IntegrityError as error:
            error_message = str(error)
            if "Duplicate entry" in error_message:
                parts = error_message.split("'")
                duplicate_value = parts[1] if len(parts) > 1 else 'value'
                raise HTTPException(409, f'{duplicate_value} already exist')

            logger.exception(
                'integrity error on create for %s',
                self.model._meta.label if self.model else '<unknown>',
            )
            raise HTTPException(409, 'Conflict creating item')

        for key in custom.keys():
            try:
                await obj._custom.aset(key.replace('custom_', ''), custom[key])
            except Exception as err:
                error_message = ' '.join(err.messages) if err.messages else str(err)
                raise HTTPException(409, error_message)

        for field in self.model._meta.many_to_many:
            if body.get(field.name) is not None:
                await save_through_model(obj, field.name, body[field.name])

        self.obj = obj
        self.obj_id = obj.id
        result = await self.get_obj(obj.id)
        return await self.return_result(result)

    async def post(self, request):
        match = re_id.match(request.path_info)
        if match:
            raise HTTPException(403, 'Path not allowed')

        result = await self.create_obj(request, self.body)

        await self.invalidate_cache(self._cache_namespaces())

        if self.normalize_obj:
            return result
        return await self.serialize(result)


class BaseTagsResource(BaseResource):

    async def add_m2m(self, result):
        super().add_m2m(result)

        if self.obj:
            result['tags'] = [tag.name async for tag in self.obj.tags.all()]

        return result


class BaseCustomResource(BaseResource):

    async def add_m2m(self, result):
        super().add_m2m(result)

        if not self.obj_id:
            return result

        fieldsets = {'default': {'name': 'Default',
                                 'order': 100000, 'fields': []}}
        cas_model = self.obj.custom_attributes.model
        cas = cas_model.objects.select_related(
            'fieldset'
        ).order_by('fieldset__order')

        has_type = False
        if hasattr(self.obj, 'card_type') and self.obj.card_type:
            has_type = True
            cas = cas.filter(
                card_type_id=self.obj.card_type.id).order_by('order')

        # custom fields definition
        fields = {}
        tmp = {}
        fieldsetId = 'default'
        cas = [ca async for ca in cas]
        for ca in cas:
            if has_type:
                if ca.presentation_id == 11:
                    fieldsetId = ca.presentation_name
                    fieldsets[fieldsetId] = {
                        'name': ca.presentation_name,
                        'order': ca.order,
                        'hide_if_empty': False,
                        'fields': []
                    }
                    continue

            else:
                if ca.fieldset:
                    fieldsetId = ca.fieldset.name
                    if fieldsetId not in fieldsets:
                        fieldsets[fieldsetId] = {
                            'name': ca.fieldset.name,
                            'order': ca.fieldset.order,
                            'hide_if_empty': ca.fieldset.hide_if_empty,
                            'fields': []
                        }
                else:
                    fieldsetId = 'default'

            fields[str(ca.id)] = model_to_dict(ca)
            tmp[ca.id] = fieldsetId

        filter = {}
        filter[
            self.obj.custom_attributes.source_field_name
        ] = self.obj.custom_attributes.instance

        # custom fields values
        cas = [
            ca async for ca in self.obj.custom_attributes.through.objects.select_related(
                'custom_attribute', 'custom_attribute__fieldset'
            ).order_by('custom_attribute__fieldset__order').filter(**filter).all()
        ]
        for ca in cas:
            fields[str(ca.custom_attribute_id)]['value'] = ca.value
            result['ca__' + ca.custom_attribute.name] = ca.value

        fieldsetId = 'default'

        for _, field in fields.items():
            fieldId = field['id']
            fieldsetId = tmp[fieldId]
            fieldsets[fieldsetId]['fields'].append(field)

        fieldsets['default'] = fieldsets.pop('default')
        result['custom_attributes'] = fieldsets

        return result
