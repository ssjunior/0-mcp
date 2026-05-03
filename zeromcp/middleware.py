import json

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .settings_helper import get_cookie_id, get_session_ttl
from .tenant.tenant import apply_session_to_request
from .util import validate_session_key
from .redis_config import KEY_PREFIX, get_redis, getex as redis_getex

COOKIE_ID = get_cookie_id()
SESSION_TTL = get_session_ttl()


class AuthMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        session_key = validate_session_key(request.COOKIES.get(COOKIE_ID))
        await apply_session_to_request(request, None)

        if session_key:
            redis = get_redis()
            session = await redis_getex(
                redis, f'{KEY_PREFIX}sessions:{session_key}', ex=SESSION_TTL,
            )
            if session:
                await apply_session_to_request(request, json.loads(session))

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
