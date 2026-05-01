import hashlib
import hmac
import re
from time import time

from .exception import HTTPException
from .redis_config import KEY_PREFIX, get_redis
from .settings_helper import get_token_max_drift_ms

_NONCE_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')


def _resolve_max_drift_ms(max_drift_ms):
    if max_drift_ms is None:
        return get_token_max_drift_ms()
    return max_drift_ms


def make_token(session_token, nonce, timestamp_ms=None):
    """Build an X-Token value from the session token, a nonce and a
    timestamp. Format: ``<timestamp_ms>.<nonce>.<hex_hmac>``.

    The HMAC is HMAC-SHA256 over ``timestamp:nonce`` keyed by
    ``session_token``. Clients can call this to mint tokens; the server
    validates with :func:`validate_token`.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time() * 1000)
    payload = f'{timestamp_ms}:{nonce}'.encode('utf-8')
    key = session_token.encode('utf-8')
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f'{timestamp_ms}.{nonce}.{digest}'


def validate_token(token, session_token, max_drift_ms=None):
    """Validate the X-Token anti-replay header.

    Token format: ``<timestamp_ms>.<nonce>.<hex_hmac>``.
    HMAC = HMAC-SHA256(session_token, "<timestamp_ms>:<nonce>").

    Raises HTTPException(403) on any failure.
    """
    max_drift_ms = _resolve_max_drift_ms(max_drift_ms)

    if not token:
        raise HTTPException(403, 'Not allowed, missing token')
    if not session_token:
        raise HTTPException(403, 'Not allowed, missing session')

    try:
        timestamp_str, nonce, signature = token.split('.', 2)
        timestamp_ms = int(timestamp_str)
    except ValueError:
        raise HTTPException(403, 'Not allowed, malformed token')

    if not _NONCE_RE.match(nonce):
        raise HTTPException(403, 'Not allowed, invalid nonce')

    if abs(timestamp_ms - int(time() * 1000)) > max_drift_ms:
        raise HTTPException(403, 'Not allowed, expired token')

    payload = f'{timestamp_ms}:{nonce}'.encode('utf-8')
    expected = hmac.new(
        session_token.encode('utf-8'), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(403, 'Not allowed, invalid signature')

    return nonce, timestamp_ms


async def consume_nonce(session_token, nonce, timestamp_ms, max_drift_ms=None):
    """Reserve nonce in Redis to prevent replay within the drift window.

    Keyed by sha256(session_token):nonce. TTL = 2 * max_drift_ms (ms).
    Raises HTTPException(403) if nonce was already used.
    """
    max_drift_ms = _resolve_max_drift_ms(max_drift_ms)

    redis = get_redis()
    sess_hash = hashlib.sha256(session_token.encode('utf-8')).hexdigest()[:16]
    key = f'{KEY_PREFIX}nonce:{sess_hash}:{nonce}'
    ttl_ms = max(max_drift_ms * 2, 1000)
    ok = await redis.set(key, str(timestamp_ms), nx=True, px=ttl_ms)
    if not ok:
        raise HTTPException(403, 'Not allowed, replayed token')


async def validate_token_async(token, session_token, max_drift_ms=None):
    """Validate X-Token and consume nonce atomically (Redis SETNX)."""
    max_drift_ms = _resolve_max_drift_ms(max_drift_ms)
    nonce, timestamp_ms = validate_token(token, session_token, max_drift_ms)
    await consume_nonce(session_token, nonce, timestamp_ms, max_drift_ms)
