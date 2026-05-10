"""Bearer token authentication.

Adds support for ``Authorization: Bearer <token>`` as an opt-in auth
method, parallel to the existing ``X-Api-Key`` and cookie session flows.

The framework is a *resource server* only: tokens are opaque, validated
by a project-supplied resolver callable. There is no ``/authorize`` /
``/token`` / ``/register`` machinery here — point ``OAUTH_AS_URL`` at an
external Authorization Server (Auth0, Keycloak, Cognito, etc.) and let
that issue tokens.

Wired into ``BaseResource._authenticate`` and ``routes._has_valid_session``;
``get_routes`` mounts the RFC 9728 discovery endpoint when configured.
"""
from __future__ import annotations

import inspect
import logging
from hashlib import sha256
from importlib import import_module
from typing import Awaitable, Callable, Optional, Tuple

from .settings_helper import (
    get_bearer_resolver,
    get_oauth_as_url,
    get_oauth_resource_url,
)
from .tenant.tenant import validate_session_shape

logger = logging.getLogger(__name__)


# Parser states for ``Authorization`` header.
ABSENT = 'absent'      # no header / non-Bearer scheme
INVALID = 'invalid'    # Bearer scheme but malformed
TOKEN = 'token'        # Bearer with a well-formed token


def parse_bearer(auth_header: Optional[str]) -> Tuple[str, Optional[str]]:
    """Parse the ``Authorization`` header into one of three states.

    Returns ``(state, token)`` where ``state`` is one of ``'absent'``,
    ``'invalid'``, or ``'token'``.

    Header is ``strip()``-ed (whitespace from proxies is benign), but
    the token itself is *not* stripped — leading/trailing whitespace
    on the token is rejected as malformed (RFC 6750 token = 1*VCHAR).

    Examples::

        '' / None              → ('absent', None)
        'Basic xyz'            → ('absent', None)
        'Bearer'               → ('invalid', None)
        'Bearer a b'           → ('invalid', None)
        'Bearer  abc'          → ('invalid', None)  # double space
        'bearer abc'           → ('token', 'abc')   # case-insensitive
        'Bearer abc'           → ('token', 'abc')
    """
    if not auth_header:
        return (ABSENT, None)
    value = auth_header.strip()
    if not value:
        return (ABSENT, None)
    parts = value.split(' ', 1)
    scheme_lower = parts[0].lower()
    if scheme_lower != 'bearer':
        return (ABSENT, None)
    if len(parts) != 2:
        return (INVALID, None)
    token = parts[1]
    # Token must not have leading/trailing whitespace, internal spaces,
    # or be empty. Normalising silently with ``strip()`` would mask bugs.
    if not token or token != token.strip() or ' ' in token:
        return (INVALID, None)
    return (TOKEN, token)


_resolver_cache: Optional[Callable] = None


def _load_resolver() -> Optional[Callable]:
    """Resolve the configured ``BEARER_RESOLVER`` callable. Cached after
    the first call. Returns ``None`` when no resolver is configured —
    callers treat that as "Bearer auth disabled"."""
    global _resolver_cache
    if _resolver_cache is not None:
        return _resolver_cache
    setting = get_bearer_resolver()
    if not setting:
        return None
    if callable(setting):
        _resolver_cache = setting
    else:
        module_path, attr = setting.rsplit('.', 1)
        module = import_module(module_path)
        _resolver_cache = getattr(module, attr)
    return _resolver_cache


def _token_hash(token: str) -> str:
    """Short hash for log messages — never log the raw token."""
    return sha256(token.encode('utf-8')).hexdigest()[:12]


# Result codes for ``resolve_bearer_session`` — let the caller decide
# 401 vs fallback based on ``REQUIRE_VALID_BEARER`` and route auth.
OK = 'ok'                      # session loaded, authenticate
FAIL_INVALID = 'invalid'       # malformed header or resolver returned None
FAIL_EXCEPTION = 'exception'   # resolver raised — operator bug
NOT_PRESENT = 'not_present'    # no Bearer header (resolver was configured)
NO_RESOLVER = 'no_resolver'    # ``BEARER_RESOLVER`` unset — Bearer disabled


async def resolve_bearer_session(
    auth_header: Optional[str],
) -> Tuple[str, Optional[dict]]:
    """Try to authenticate via Bearer. Returns ``(code, session)``.

    - ``NO_RESOLVER``: ``BEARER_RESOLVER`` unset — Bearer auth is
      disabled. Caller MUST treat this as "Bearer module not active"
      and never trigger strict mode based on it (preserves 1.3.x
      behaviour for projects that haven't opted in).
    - ``NOT_PRESENT``: resolver configured but no Bearer header on the
      request — caller falls through to other auth methods.
    - ``OK``: valid bearer + session dict that passed shape validation.
    - ``FAIL_INVALID``: header malformed, resolver returned None, or
      resolver returned a session dict that failed shape validation.
    - ``FAIL_EXCEPTION``: resolver raised. Logged with token hash and
      exception type only; the exception message and traceback are
      never logged because they may contain the raw token.
    """
    resolver = _load_resolver()
    if resolver is None:
        return (NO_RESOLVER, None)

    state, token = parse_bearer(auth_header)
    if state == ABSENT:
        return (NOT_PRESENT, None)
    if state == INVALID:
        return (FAIL_INVALID, None)

    try:
        result = resolver(token)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        # Deliberately no ``exc_info=True``: a resolver might raise
        # ``RuntimeError(f"bad token {token}")``, which would put the
        # raw token in the traceback. We log the type name and a hash
        # so the operator can correlate, then swallow the rest.
        logger.warning(
            'bearer resolver raised %s for token %s',
            type(exc).__name__, _token_hash(token),
        )
        return (FAIL_EXCEPTION, None)

    if result is None:
        return (FAIL_INVALID, None)

    if not validate_session_shape(result, resolver):
        return (FAIL_INVALID, None)

    return (OK, result)


# ── RFC 9728 discovery ───────────────────────────────────────────────


class OAuthDiscoveryMisconfigured(Exception):
    """Raised at ``get_routes()`` time when ``OAUTH_AS_URL`` is set
    without ``OAUTH_RESOURCE_URL``. We refuse to emit a half-empty
    discovery document — fail in the boot path instead."""


def discovery_metadata() -> dict:
    """Build the RFC 9728 ``oauth-protected-resource`` document."""
    as_url = get_oauth_as_url()
    resource_url = get_oauth_resource_url()
    if as_url and not resource_url:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            "MCP['OAUTH_AS_URL'] is set but MCP['OAUTH_RESOURCE_URL'] "
            "is missing. RFC 9728 requires the canonical resource "
            "identifier — set both, or neither."
        )
    return {
        'resource': resource_url,
        'authorization_servers': [as_url],
        'bearer_methods_supported': ['header'],
    }


__all__ = [
    'parse_bearer', 'resolve_bearer_session', 'discovery_metadata',
    'ABSENT', 'INVALID', 'TOKEN',
    'OK', 'FAIL_INVALID', 'FAIL_EXCEPTION', 'NOT_PRESENT', 'NO_RESOLVER',
]
