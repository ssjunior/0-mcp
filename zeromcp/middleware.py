import json

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .settings_helper import get_cookie_id
from .tenant.tenant import aset_tenant
from .util import validate_session_key
from .redis_config import KEY_PREFIX, get_redis

COOKIE_ID = get_cookie_id()


class AuthMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        session_key = validate_session_key(request.COOKIES.get(COOKIE_ID))
        request.account_id = None
        request.authenticated = None
        request.session = None

        if session_key:
            redis = get_redis()
            session_key = f'{KEY_PREFIX}sessions:{session_key}'
            session = await redis.get(session_key)

            if session:
                session = json.loads(session)
                request.user = session['user']
                request.authenticated = bool(session.get('user'))
                request.session = session
                if session.get('account'):
                    await aset_tenant(session['account']['id'])
                    request.account = session['account']
                    request.account_id = session['account']['id']

        response = await self.get_response(request)
        return response


class ExceptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, _, exception):
        try:
            getattr(exception, "render")
        except AttributeError:
            return None

        return exception.render(exception)
