"""Dispatch-time wiring of P0 rate-limit features.

Two paths:
  • credential-path auto-defense — ``CREDENTIAL_PATHS`` prefix match
    triggers a per-IP narrow bucket on top of ``api/login`` enforcement.
  • declarative ``rate_limits`` on a Resource — runs after auth so
    ``key`` callables can read ``self.user``."""
import pytest
from django.test import override_settings

from zeromcp import base as base_module
from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


@pytest.fixture
def loose_api_limits(monkeypatch):
    """The default ``api`` bucket allows only 4 hits/s; tests need to
    fire several hits in a tight loop without tripping the api ceiling
    (we want to test the *credential* bucket isolation)."""
    monkeypatch.setattr(base_module, 'RATE_LIMITS', {
        'api':   [{'interval': 1000, 'limit': 1000}],
        'login': [{'interval': 1000, 'limit': 1000}],
        'abuse': [{'interval': 1000, 'limit': 100000}],
    })


class _FakeGet:
    def get(self, key, default=None):
        return default

    def urlencode(self):
        return ''

    def __iter__(self):
        return iter([])

    def __contains__(self, key):
        return False


class FakeRequest:
    body = b'{}'
    content_type = 'application/json'
    headers = {}
    COOKIES = {}

    def __init__(self, path='/api/test', method='GET', remote='1.1.1.1'):
        self.method = method
        self.path = path
        self.path_info = path
        self.GET = _FakeGet()
        self.META = {'REMOTE_ADDR': remote}


class CredentialResource(BaseResource):
    model = Space
    authenticated = False

    async def post(self, request):
        return {'ok': True}


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_credential_path_blocks_after_5_hits_from_same_ip(fake_redis, loose_api_limits):
    """Default ``CREDENTIAL_RATE_LIMIT`` is 5 hits / 30s. Sixth hit on
    ``/forgot`` from the same IP returns 429 even though the regular
    ``api`` bucket is much wider."""
    for _ in range(5):
        res = CredentialResource()
        await res.dispatch(FakeRequest(path='/forgot', method='POST', remote='9.9.9.9'))

    res = CredentialResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(FakeRequest(path='/forgot', method='POST', remote='9.9.9.9'))
    assert exc.value.args[0] == 429


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_credential_segment_match_covers_nested_paths(fake_redis, loose_api_limits):
    """``startswith`` was too strict. Nested paths must match too —
    ``/login/forgot`` and ``/user/change_password`` are real-world
    routes from consumer projects (Lufthansa portal)."""
    for _ in range(5):
        res = CredentialResource()
        await res.dispatch(FakeRequest(path='/login/forgot', method='POST', remote='6.6.6.6'))

    res = CredentialResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(FakeRequest(path='/login/forgot', method='POST', remote='6.6.6.6'))
    assert exc.value.args[0] == 429


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_credential_segment_match_does_not_overmatch_similar_words(fake_redis, loose_api_limits):
    """``/forgotten`` is not a credential path — ``forgotten`` is a
    different segment from ``forgot``. No bucket pressure."""
    for _ in range(20):
        res = CredentialResource()
        response = await res.dispatch(FakeRequest(path='/forgotten/items', method='POST', remote='2.2.2.2'))
        assert response.status_code == 200


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_credential_path_does_not_block_other_path(fake_redis, loose_api_limits):
    """The credential bucket is keyed by IP+path-class — hits on
    ``/forgot`` don't poison ``/api/test`` for the same IP."""
    for _ in range(5):
        res = CredentialResource()
        await res.dispatch(FakeRequest(path='/forgot', method='POST', remote='8.8.8.8'))

    # ``/api/test`` from same IP — credential bucket doesn't apply, regular
    # api bucket allows several requests in this window.
    res = CredentialResource()
    response = await res.dispatch(FakeRequest(path='/api/test', method='POST', remote='8.8.8.8'))
    assert response.status_code == 200


class DeclarativeResource(BaseResource):
    model = Space
    authenticated = False
    rate_limits = [
        {'name': 'short', 'key': lambda r: r.identifier,
         'limit': 2, 'window': 60},
    ]

    async def get(self, request):
        return {'ok': True}


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_declarative_rate_limit_blocks_after_limit(fake_redis, loose_api_limits):
    for _ in range(2):
        res = DeclarativeResource()
        response = await res.dispatch(FakeRequest(remote='3.3.3.3'))
        assert response.status_code == 200

    res = DeclarativeResource()
    with pytest.raises(HTTPException) as exc:
        await res.dispatch(FakeRequest(remote='3.3.3.3'))
    assert exc.value.args[0] == 429


class PerUserResource(BaseResource):
    model = Space
    authenticated = False
    rate_limits = [
        {'name': 'per_user', 'key': lambda r: (r.user or {}).get('id'),
         'limit': 1, 'window': 60},
    ]

    async def get(self, request):
        return {'ok': True}


@pytest.mark.asyncio
@override_settings(DEBUG=False)
async def test_per_user_rate_limit_skipped_when_user_absent(fake_redis, loose_api_limits):
    """Anonymous request — ``key`` callable returns ``None``, the bucket
    silently skips. NAT scenario where per-IP would have over-blocked."""
    for _ in range(5):
        res = PerUserResource()
        response = await res.dispatch(FakeRequest(remote='4.4.4.4'))
        assert response.status_code == 200


@pytest.mark.asyncio
@override_settings(DEBUG=True)
async def test_debug_bypasses_credential_and_declarative(fake_redis):
    """``DEBUG = True`` short-circuits both new hooks (matches the
    behaviour of the legacy api/login enforcement)."""
    for _ in range(50):
        res = CredentialResource()
        await res.dispatch(FakeRequest(path='/forgot', method='POST', remote='5.5.5.5'))
    # Still 200, no 429 — debug bypassed everything.
    res = CredentialResource()
    response = await res.dispatch(FakeRequest(path='/forgot', method='POST', remote='5.5.5.5'))
    assert response.status_code == 200
