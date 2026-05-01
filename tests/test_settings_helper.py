"""Helper that reads 0-mcp configuration from ``settings.MCP``.

Resolution: ``settings.MCP[name]`` → ``settings.MCP[<unprefixed>]``
→ ``default``. No legacy fallback to top-level vars — projects keep
all 0-mcp config inside the bag.
"""
from settings import settings as project_settings

from zeromcp.settings_helper import get_cookie_id, get_setting, get_token_max_drift_ms


def test_value_in_bag(monkeypatch):
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'CACHE_TTL': 300,
    })
    assert get_setting('CACHE_TTL') == 300


def test_missing_key_returns_default(monkeypatch):
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'OTHER': 1,
    })
    assert get_setting('CACHE_TTL', default=120) == 120


def test_no_bag_returns_default(monkeypatch):
    monkeypatch.delattr(project_settings, 'MCP', raising=False)
    assert get_setting('CACHE_TTL', default=120) == 120


def test_default_is_none_when_not_provided(monkeypatch):
    monkeypatch.delattr(project_settings, 'MCP', raising=False)
    assert get_setting('NONEXISTENT') is None


def test_falsy_value_is_returned_not_treated_as_missing(monkeypatch):
    """``False``, ``0``, ``''`` are valid configured values."""
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'CACHE_TTL_ENABLE': False,
    })
    assert get_setting('CACHE_TTL_ENABLE', default=True) is False


def test_mcp_prefix_is_redundant_inside_bag(monkeypatch):
    """``MCP_FOO`` and ``FOO`` are equivalent inside the bag —
    the namespace prefix is noise once already inside the dict."""
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'API_KEY_RESOLVER': 'a',
    })
    assert get_setting('MCP_API_KEY_RESOLVER') == 'a'

    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'MCP_API_KEY_RESOLVER': 'b',
    })
    assert get_setting('MCP_API_KEY_RESOLVER') == 'b'


def test_full_name_wins_over_unprefixed_when_both_present(monkeypatch):
    """If a project has both forms, the exact name wins."""
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={
        'MCP_API_KEY_RESOLVER': 'with-prefix',
        'API_KEY_RESOLVER': 'without-prefix',
    })
    assert get_setting('MCP_API_KEY_RESOLVER') == 'with-prefix'


def test_non_dict_settings_falls_through_to_default(monkeypatch):
    """``MCP`` set to something other than a dict is ignored."""
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value='oops')
    assert get_setting('CACHE_TTL', default=120) == 120


def test_cookie_id_default_is_canonical(monkeypatch):
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={})
    assert get_cookie_id() == 'sessionid'


def test_token_max_drift_default_is_canonical(monkeypatch):
    monkeypatch.setattr(project_settings, 'MCP', raising=False, value={})
    assert get_token_max_drift_ms() == 30000
