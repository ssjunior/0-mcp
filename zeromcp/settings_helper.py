"""Read 0-mcp configuration from the project's ``MCP`` dict.

A single dict named ``MCP`` in ``settings.py`` collects every
0-mcp-owned setting (DRF/Celery-style namespace)::

    MCP = {
        'CACHE_TTL': 300,
        'ENFORCE_TOKEN': True,
        'RATE_LIMITS': {...},
    }

Inside the bag the historical ``MCP_`` prefix is redundant:
``MCP_API_KEY_RESOLVER`` and ``API_KEY_RESOLVER`` resolve to the
same setting.
"""


def get_setting(name, default=None):
    """Look up a setting by name in ``settings.MCP``.

    Returns ``default`` (which itself defaults to ``None``) when the
    project hasn't defined the bag or the key is missing.
    """
    try:
        from settings import settings as _project_settings
    except ImportError:
        return default

    bag = getattr(_project_settings, 'MCP', None)
    if not isinstance(bag, dict):
        return default

    for key in (name, name.removeprefix('MCP_')):
        if key in bag:
            return bag[key]

    return default


# Single canonical defaults used by every entry point in the framework.
# Reading these via ``get_cookie_id()`` / ``get_token_max_drift_ms()``
# avoids per-module defaults drifting (``ws.py`` previously defaulted to
# ``'sid'`` while everything else used ``'sessionid'``).
def get_cookie_id():
    return get_setting('COOKIE_ID', default='sessionid')


def get_token_max_drift_ms():
    return get_setting('TOKEN_MAX_DRIFT_MS', default=30000)


def get_session_ttl():
    return get_setting('SESSION_TTL', default=1800)


def get_api_session_ttl():
    return get_setting('API_SESSION_TTL', default=300)


def get_read_only():
    return get_setting('READ_ONLY', default=False)


def get_credential_paths():
    """Path-segment list that triggers the credential-path rate-limit
    auto-defense. Anything matching one of these segments — at the
    root or nested anywhere in the path — gets the tight
    ``CREDENTIAL_RATE_LIMIT`` bucket per-IP on top of the regular API
    bucket.

    Match is segment-based (regex ``(^|/){p}(/|$)``), so:
      * ``/forgot``               → matched
      * ``/login/forgot``         → matched
      * ``/user/change_password`` → matched
      * ``/forgotten``            → NOT matched (different segment)

    Configure with leading slash for readability — it's stripped at
    match time. Set to ``[]`` in ``MCP`` to disable.

    **Override semantics:** ``MCP['CREDENTIAL_PATHS']`` *replaces* the
    framework default — does not merge. To add project-specific paths
    while keeping the defaults, copy the default list and extend::

        MCP = {
            'CREDENTIAL_PATHS': [
                '/forgot', '/change_password', '/signup',
                '/reset', '/recover', '/register',  # default
                '/activate', '/email_validate',     # extras
            ],
        }
    """
    return get_setting('CREDENTIAL_PATHS', default=[
        '/forgot', '/change_password', '/signup',
        '/reset', '/recover', '/register',
    ])


def get_credential_rate_limit():
    """Per-IP rate-limit applied to ``CREDENTIAL_PATHS``. ``window`` is
    in seconds. Tighter than the default ``api`` bucket because these
    paths are the natural targets for credential-stuffing and password
    reset abuse — but loose enough to absorb legitimate retries
    (re-send email button, NAT-shared offices)."""
    return get_setting('CREDENTIAL_RATE_LIMIT', default={
        'limit': 5, 'window': 30,
    })


def get_bearer_resolver():
    """Dotted path or callable that resolves a bearer token to a session
    dict. Same shape as the X-Api-Key resolver: ``{'user': {...},
    'account': {...}}`` (account is multi-tenant only).

    When unset, the framework treats ``Authorization: Bearer`` headers
    as absent — behavior identical to 1.3.x.
    """
    return get_setting('BEARER_RESOLVER')


def get_require_valid_bearer():
    """Strict mode for Bearer auth on routes with ``authenticated=True``.

    When ``True``: the only accepted credential is a valid bearer token.
    Missing or malformed Bearer headers, and tokens the resolver rejects,
    return 401 — fallback to ``X-Api-Key`` and cookie session is disabled.
    Public routes (``authenticated=False``) are unaffected.

    When ``False`` (default): missing or invalid bearer falls back to the
    other auth methods. A resolver exception still maps to 401 — that's
    operator/resolver bug, not a malformed credential.
    """
    return get_setting('REQUIRE_VALID_BEARER', default=False)


def get_oauth_as_url():
    """URL of the external OAuth Authorization Server.

    When set, the framework mounts a discovery endpoint at
    ``/.well-known/oauth-protected-resource`` (RFC 9728) so MCP clients
    can locate the AS. ``OAUTH_RESOURCE_URL`` becomes mandatory.
    """
    return get_setting('OAUTH_AS_URL')


def get_oauth_resource_url():
    """Canonical resource identifier emitted in the RFC 9728 metadata.

    Required when ``OAUTH_AS_URL`` is set. Should be the absolute URL
    of this resource server (e.g. ``https://api.cliente.com``).
    """
    return get_setting('OAUTH_RESOURCE_URL')
