from datetime import datetime, tzinfo

from django.core.serializers.json import DjangoJSONEncoder


def _resolve_tz(tz) -> tzinfo | None:
    """Accept either a tzinfo instance or a name string.

    Strings flow in from Django settings (``USE_TZ`` + ``TIME_ZONE``)
    or from ``with_timezone('America/Sao_Paulo')`` calls in handlers.
    Returns ``None`` when the input is already ``None`` so callers
    can pass through naïve datetimes untouched.
    """
    if tz is None or isinstance(tz, tzinfo):
        return tz
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(tz))
    except Exception:
        try:
            import pytz  # type: ignore
            return pytz.timezone(str(tz))
        except Exception:
            return None


class CustomJSONEncoder(DjangoJSONEncoder):
    timezone = 'UTC'

    @classmethod
    def with_timezone(cls, tz):
        return type('CustomJSONEncoder', (cls,), {'timezone': tz})

    def default(self, obj, **kwargs):
        if isinstance(obj, datetime):
            tz = _resolve_tz(self.timezone)
            try:
                if tz is not None and obj.tzinfo is not None:
                    obj = obj.astimezone(tz)
            except Exception:
                # Naïve datetime + tz mismatch — fall through and emit
                # the original timestamp; better a slightly wrong
                # display than a 500.
                pass
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        return super().default(obj)
