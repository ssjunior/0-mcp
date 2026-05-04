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
