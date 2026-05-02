"""Shared helper that applies a session dict (or anonymous shape) onto
``request`` and synchronizes the tenant context. Used by both the
cookie middleware and ``BaseResource._authenticate`` so all auth methods
produce the same downstream request shape.
"""
import types

import pytest

from zeromcp.tenant import tenant


def _bare_request():
    """Minimal stand-in for an HTTP request — mutable namespace, no
    Django dependency. The helper only sets attributes."""
    return types.SimpleNamespace()


@pytest.fixture
def reset_db_state():
    tenant.db_state.set('default')
    yield
    tenant.db_state.set('default')


@pytest.mark.asyncio
async def test_anonymous_shape_resets_all_attributes(reset_db_state):
    request = _bare_request()
    await tenant.apply_session_to_request(request, None)
    assert request.user is None
    assert request.session is None
    assert request.authenticated is False
    assert request.account is None
    assert request.account_id is None
    assert tenant.db_state.get() == 'default'


@pytest.mark.asyncio
async def test_session_without_account_clears_tenant(
    monkeypatch, reset_db_state,
):
    """Single-tenant or account-less api-key must NOT inherit a tenant
    from a previous middleware/auth pass on the same request."""
    tenant.db_state.set('tenant_42')
    request = _bare_request()
    session = {'user': {'id': 1, 'email': 'x@y.com'}}
    await tenant.apply_session_to_request(request, session)

    assert request.user == session['user']
    assert request.session is session
    assert request.authenticated is True
    assert request.account is None
    assert request.account_id is None
    assert tenant.db_state.get() == 'default'


@pytest.mark.asyncio
async def test_anonymous_clears_previously_set_account(reset_db_state):
    """Calling helper with None after a populated state must wipe
    account too — covers identity-mixing scenarios."""
    request = _bare_request()
    request.user = {'id': 1}
    request.account = {'id': 99}
    request.account_id = 99
    request.session = {'user': {'id': 1}, 'account': {'id': 99}}
    request.authenticated = True
    tenant.db_state.set('tenant_99')

    await tenant.apply_session_to_request(request, None)

    assert request.user is None
    assert request.session is None
    assert request.authenticated is False
    assert request.account is None
    assert request.account_id is None
    assert tenant.db_state.get() == 'default'


@pytest.mark.asyncio
async def test_clear_tenant_resets_db_state(reset_db_state):
    tenant.db_state.set('tenant_5')
    tenant.clear_tenant()
    assert tenant.db_state.get() == 'default'


@pytest.mark.asyncio
async def test_helper_is_idempotent(reset_db_state):
    request = _bare_request()
    session = {'user': {'id': 1}}
    await tenant.apply_session_to_request(request, session)
    await tenant.apply_session_to_request(request, session)
    assert request.user == session['user']
    assert request.authenticated is True
