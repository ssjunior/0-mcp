import logging

from django.http import JsonResponse

logger = logging.getLogger(__name__)


class HTTPException(Exception):

    def render(self, exception):
        (status, detail) = exception.args
        # 5xx is a server-side problem — emit a logged stack trace so the
        # response body is not the only signal. Without this, every
        # ``raise HTTPException(500, ...)`` was invisible to logs/Sentry
        # and operators had to guess from black-box symptoms.
        if isinstance(status, int) and status >= 500:
            logger.exception(
                'HTTPException %s: %s', status, detail,
                exc_info=exception,
            )
        return JsonResponse(
            {'success': False, 'status': status, 'detail': detail},
            status=status,
        )
