from contextvars import ContextVar
import json
import logging
import os
from hashlib import sha256

from asgiref.sync import async_to_sync
import django
from django.apps import apps
from django.contrib.auth.hashers import check_password
from django.db.models import Q
from ..exception import HTTPException
from ..redis_config import KEY_PREFIX, get_redis
from .registry import TenantConnectionRegistry

logger = logging.getLogger(__name__)

# Multi-tenant configuration is optional. Projects that don't use it
# can omit any (or all) of these names from ``settings`` — defaults
# below produce a single-DB setup where ``aset_tenant`` is a no-op.
try:
    from settings import DEFAULT_DATABASE  # type: ignore[attr-defined]
except ImportError:
    try:
        from django.conf import settings as _django_settings
        DEFAULT_DATABASE = getattr(_django_settings, 'DATABASES', {}).get('default', {})
    except Exception:
        # Likely running ``0-mcp init`` (or another standalone CLI flow)
        # before any Django project exists — no settings, no problem.
        DEFAULT_DATABASE = {}

try:
    from settings import TENANT_ACCOUNT_MODEL  # type: ignore[attr-defined]
except ImportError:
    TENANT_ACCOUNT_MODEL = ''

try:
    from settings import TENANT_USER_MODEL  # type: ignore[attr-defined]
except ImportError:
    TENANT_USER_MODEL = ''

try:
    from settings import TENANT_DB_PREFIX  # type: ignore[attr-defined]
except ImportError:
    TENANT_DB_PREFIX = ''

try:
    from settings import TENANT_USER_API_MODEL  # type: ignore[attr-defined]
except ImportError:
    TENANT_USER_API_MODEL = ''

try:
    from settings import HASH_LENGTH  # type: ignore[attr-defined]
except ImportError:
    HASH_LENGTH = 8

from ..settings_helper import get_api_session_ttl, get_setting

API_SESSION_TTL = get_api_session_ttl()
MCP_API_KEY_RESOLVER = get_setting('MCP_API_KEY_RESOLVER')


if not apps.ready:
    DJANGO_SETTINGS_MODULE = os.getenv('DJANGO_SETTINGS_MODULE')
    if not DJANGO_SETTINGS_MODULE:
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')
    django.setup()


class ACCOUNT_STATUS():
    """Possible account database statuses.

    Attributes:
        ACTIVE (int): Account active with all features enabled.
        CREATING_DATABASE (int): Account is being provisioned.
        DATABASE_CREATION_ERROR (int): Error occurred while provisioning.
        UPDATING_DATABASE (int): Database is being migrated.
        DELETED (int): Account removed; access no longer allowed.
        WAITING_DELETION (int): Marked for future removal by a script.
        OPTIONS (dict): Mapping of status to label.
        CHOICES (tuple): Status/label tuples for use in models.
    """
    ACTIVE = 1
    DISABLED = 2
    FREE = 3
    DELETED = 4
    PAUSED = 5
    CREATING_DATABASE = 6
    DATABASE_CREATION_ERROR = 7

    OPTIONS = {
        ACTIVE: 'Active',
        DISABLED: 'Disabled',
        FREE: 'Waiting allocation',
        DELETED: 'Deleted',
        PAUSED: 'Paused',
        CREATING_DATABASE: 'Creating database',
        DATABASE_CREATION_ERROR: 'Database creation error',
    }
    CHOICES = tuple(OPTIONS.items())


try:
    tenant_model = apps.get_model(TENANT_USER_MODEL)
except Exception:
    TENANT_USER_MODEL = None
    tenant_model = None

try:
    user_api_model = apps.get_model(TENANT_USER_API_MODEL)
except Exception:
    TENANT_USER_API_MODEL = None
    user_api_model = None

try:
    account_model = apps.get_model(TENANT_ACCOUNT_MODEL)
except Exception:
    TENANT_ACCOUNT_MODEL = None
    account_model = None

db_state = ContextVar("db_state", default='default')


registry = TenantConnectionRegistry(
    db_prefix=TENANT_DB_PREFIX,
    default_database=DEFAULT_DATABASE,
    account_model_getter=lambda: account_model,
)


def _build_connection(account_db, account):
    """Compatibility wrapper — delegates to the registry."""
    return registry._build_connection(account_db, account)


async def _load_connection(account_id, account=None):
    """Compatibility wrapper — delegates to the registry.

    Returns just the connection dict (no alias). New code should call
    ``registry.get_or_create`` directly.
    """
    cached = await registry.get_cached_connection(account_id)
    if cached is not None:
        return cached

    if account is None:
        account = await account_model.objects.filter(
            id=account_id,
        ).select_related('db').afirst()

    alias = registry.make_alias(account_id)
    connection = registry._build_connection(alias, account)
    await registry.cache_connection(account_id, connection)
    return connection


async def save_connection(account):
    alias, _ = await registry.get_or_create(account.id, account)
    db_state.set(alias)


async def set_default(id):
    """SCRIPT-ONLY. Mutates the global ``default`` DB connection.

    Not safe inside ASGI request handling — affects every concurrent
    request in the process. Use only from one-off scripts/management
    commands. Prefer ``aset_tenant`` for per-request tenant switching.
    """
    await registry.set_default_connection(id)


async def unset_default(id):
    """SCRIPT-ONLY. Restores the global ``default`` DB connection.

    See :func:`set_default` for caveats.
    """
    registry.restore_default_connection()


def get_tenant():
    account_db = db_state.get()
    if not account_db or account_db == 'default':
        return None
    # Alias is "<TENANT_DB_PREFIX>_<id>". rsplit so prefixes containing
    # underscores (e.g. ``my_tenant``) still parse correctly.
    return account_db.rsplit('_', 1)[1]


async def aset_tenant(id):
    alias = registry.make_alias(id)

    if not registry.is_registered(alias):
        account = await account_model.objects.filter(
            id=id,
        ).select_related('db').afirst()

        if not account:
            raise HTTPException(400, 'Missing account')

        await registry.get_or_create(id, account)

    db_state.set(alias)
    return alias


def clear_tenant():
    """Reset tenant context to the framework default. Used when a
    request transitions from an authenticated tenant-bound state to
    anonymous or to a session without account, so subsequent ORM calls
    in the same request don't keep the previous tenant's DB alias.
    """
    db_state.set('default')


async def apply_session_to_request(request, session):
    """Mirror the cookie-auth shape onto ``request`` for any auth path.

    Used by both ``AuthMiddleware`` and ``BaseResource._authenticate``
    so api-key, cookie, and any future auth method produce identical
    request attributes and identical tenant activation. Idempotent —
    safe to call twice in the same request.

    ``session=None`` resets the request to the anonymous shape and
    clears the tenant context.
    """
    if session is None:
        request.user = None
        request.session = None
        request.authenticated = False
        request.account = None
        request.account_id = None
        clear_tenant()
        return

    request.user = session['user']
    request.session = session
    request.authenticated = bool(session.get('user'))
    account = session.get('account')
    request.account = account
    request.account_id = account['id'] if account else None
    if account:
        await aset_tenant(account['id'])
    else:
        clear_tenant()


def _build_session(user, account_id):
    """Shared session-dict builder. Accepts ``account_id=None`` for
    single-tenant projects so the same shape works in both modes."""
    avatar = getattr(user, 'avatar', None)
    avatar_url = (
        f'/shared/{account_id}/avatar/{avatar}'
        if avatar and account_id else
        (f'/shared/avatar/{avatar}' if avatar else None)
    )
    user_dict = {
        'id': user.id,
        'avatar': avatar_url,
        'email': getattr(user, 'email', None),
        'is_admin': getattr(user, 'is_admin', False),
        'is_owner': getattr(user, 'is_owner', False),
        'locale': getattr(user, 'locale', None),
        'name': getattr(user, 'name', None),
        'preferences': getattr(user, 'preferences', None),
        'timezone': getattr(user, 'timezone', None),
    }
    if account_id is not None:
        user_dict['account_id'] = account_id
    session = {'user': user_dict}
    if account_id is not None:
        session['account_id'] = account_id
        session['account'] = {'id': account_id}
    return session


async def _default_resolve_api_key(api_key):
    """Default API key resolver — works in two modes.

    **Multi-tenant mode** (active when ``TENANT_ACCOUNT_MODEL`` is
    configured): expects the four-segment self-describing format
    ``<account_id>.<uuid_part>.<salt>.<hash_part>``, validates the
    hash, switches to the tenant DB, and looks the key up in
    ``user_api_model``.

    **Single-tenant mode** (no ``TENANT_ACCOUNT_MODEL``): treats the
    key as an opaque token and looks it up in ``user_api_model`` on
    the default database. No format constraints, no tenant switching.

    Returns a session dict on success, ``None`` on any failure.
    """
    if user_api_model is None:
        return None

    if account_model is None:
        # Single-tenant fast path — opaque token lookup.
        user = await user_api_model.objects.filter(api_key=api_key).afirst()
        if not user:
            return None
        return _build_session(user, account_id=None)

    # Multi-tenant: parse and validate the self-describing format.
    try:
        account_id, uuid_part, salt, hash_part = api_key.split('.', 3)
        hash_input = f"{account_id}{uuid_part}{salt}".encode('utf-8')
        recalculated_hash = sha256(hash_input).hexdigest()[:HASH_LENGTH]
        if recalculated_hash != hash_part:
            return None
    except ValueError:
        return None

    await aset_tenant(account_id)
    user = await user_api_model.objects.filter(api_key=api_key).afirst()
    if not user:
        return None
    return _build_session(user, account_id=account_id)


_resolver_cache = None


def _load_resolver():
    """Resolve the configured ``MCP_API_KEY_RESOLVER`` callable, or
    fall back to the default. Cached after the first call."""
    global _resolver_cache
    if _resolver_cache is not None:
        return _resolver_cache
    if not MCP_API_KEY_RESOLVER:
        _resolver_cache = _default_resolve_api_key
        return _resolver_cache
    from importlib import import_module
    if callable(MCP_API_KEY_RESOLVER):
        _resolver_cache = MCP_API_KEY_RESOLVER
    else:
        module_path, attr = MCP_API_KEY_RESOLVER.rsplit('.', 1)
        module = import_module(module_path)
        _resolver_cache = getattr(module, attr)
    return _resolver_cache


async def get_api_session(api_key):
    """Resolve an API key to a session dict.

    Reads from Redis first; on miss, delegates to the configured
    ``MCP_API_KEY_RESOLVER`` callable (defaults to
    :func:`_default_resolve_api_key`, which expects the legacy
    self-describing four-segment format). Custom resolvers receive
    the raw key and return either a session dict or ``None``.

    The result is cached under ``api_session:<sha256(api_key)[:16]>`` with
    a sliding ``API_SESSION_TTL``-second window — each hit uses ``GETEX``
    to read and renew the TTL atomically, so an idle key expires at
    ``API_SESSION_TTL`` after its last use. Requires Redis >= 6.2.
    """
    redis = get_redis()
    key_hash = sha256(api_key.encode('utf-8')).hexdigest()[:16]
    session_key = f'{KEY_PREFIX}api_session:{key_hash}'
    cached = await redis.getex(session_key, ex=API_SESSION_TTL)
    if cached:
        session = json.loads(cached)
        # Revalidate on read — a cached entry primed by older code or a
        # since-tightened contract must not bypass the current rules.
        if _validate_session_shape(session, resolver=None):
            return session
        await redis.delete(session_key)

    resolver = _load_resolver()
    session = await resolver(api_key)
    if session is None:
        return None
    if not _validate_session_shape(session, resolver):
        return None

    await redis.setex(session_key, API_SESSION_TTL, json.dumps(session))
    return session


def _validate_session_shape(session, resolver):
    """Strict shape check for resolver output (and cached entries).

    Required: ``session`` is a dict with a ``user`` dict.
    Multi-tenant (``account_model`` configured): ``account`` must be a
    dict with an ``id``; if a top-level ``account_id`` is also present,
    it must equal ``account['id']`` (no divergent values).
    Single-tenant: ``account``/``account_id`` MUST NOT be present —
    tenant data in a non-multi-tenant project signals a contract bug.

    ``resolver=None`` means we are validating a cached entry (origin
    unknown) — the message names ``cached entry`` instead.
    """
    source = (
        getattr(resolver, '__qualname__', repr(resolver))
        if resolver is not None else 'cached api-key entry'
    )
    if not isinstance(session, dict):
        logger.warning('%s is not a dict', source)
        return False
    user = session.get('user')
    if not isinstance(user, dict):
        logger.warning('%s has no dict user', source)
        return False
    if account_model is not None:
        account = session.get('account')
        if not isinstance(account, dict) or 'id' not in account:
            logger.warning(
                "%s has no dict account['id'] (multi-tenant)", source,
            )
            return False
        top_id = session.get('account_id')
        if top_id is not None and str(top_id) != str(account['id']):
            logger.warning(
                "%s has divergent account_id (%r) and account['id'] (%r)",
                source, top_id, account['id'],
            )
            return False
    else:
        if 'account' in session or 'account_id' in session:
            logger.warning(
                '%s carries account data in single-tenant project — '
                'rejecting', source,
            )
            return False
    return True


def set_tenant(id):
    sync = async_to_sync(aset_tenant)
    return sync(id)


async def get_master_user(email, password):
    user = await tenant_model.objects.using(
        'default'
    ).filter(
        email=email.lower().strip(),
    ).select_related(
        'account', 'account__db'
    ).filter(
        account__status_id__in=[
            ACCOUNT_STATUS.ACTIVE,
        ]
    ).afirst()

    if user and check_password(password, user.password):
        if not user.account:
            raise HTTPException(400, 'Missing account')

        await save_connection(user.account)

        return user

    return None


async def get_account(domain):
    if not account_model or not domain:
        raise HTTPException(400, 'Missing data')

    domain = domain.lower().strip()

    account = await account_model.objects.using(
        'default'
    ).filter(
        Q(subdomain=domain) | Q(domain=domain),
    ).select_related(
        'db'
    ).filter(
        status_id__in=[
            ACCOUNT_STATUS.ACTIVE,
        ]
    ).afirst()

    if account:
        await save_connection(account)
        return account

    return None
