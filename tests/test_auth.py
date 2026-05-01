import hashlib
import hmac
from time import time

import pytest

from zeromcp.auth import (
    consume_nonce,
    make_token,
    validate_token,
    validate_token_async,
)
from zeromcp.exception import HTTPException


def test_missing_token_raises():
    with pytest.raises(HTTPException) as exc:
        validate_token(None, 'sess123')
    assert exc.value.args[0] == 403
    assert 'missing token' in exc.value.args[1]


def test_missing_session_token_raises():
    with pytest.raises(HTTPException) as exc:
        validate_token('1.n.deadbeef', None)
    assert exc.value.args[0] == 403
    assert 'missing session' in exc.value.args[1]


def test_valid_token():
    sess = 'sess12345'
    token = make_token(sess, nonce='abc123')
    validate_token(token, sess)


def test_expired_token():
    sess = 'sess12345'
    ts = int(time() * 1000) - 60_000
    token = make_token(sess, nonce='abc', timestamp_ms=ts)
    with pytest.raises(HTTPException) as exc:
        validate_token(token, sess)
    assert exc.value.args[0] == 403
    assert 'expired' in exc.value.args[1]


def test_default_drift_allows_reasonable_clock_skew():
    sess = 'sess12345'
    ts = int(time() * 1000) - 20_000
    token = make_token(sess, nonce='abc', timestamp_ms=ts)
    validate_token(token, sess)


def test_explicit_drift_override_remains_strict():
    sess = 'sess12345'
    ts = int(time() * 1000) - 20_000
    token = make_token(sess, nonce='abc', timestamp_ms=ts)
    with pytest.raises(HTTPException) as exc:
        validate_token(token, sess, max_drift_ms=5000)
    assert exc.value.args[0] == 403
    assert 'expired' in exc.value.args[1]


def test_garbage_token_raises_malformed():
    with pytest.raises(HTTPException) as exc:
        validate_token('!!!not-a-token!!!', 'sess12345')
    assert exc.value.args[0] == 403
    assert 'malformed' in exc.value.args[1]


def test_wrong_session_token_raises():
    sess = 'sess12345'
    token = make_token(sess, nonce='abc')
    with pytest.raises(HTTPException) as exc:
        validate_token(token, 'differentsession')
    assert 'invalid signature' in exc.value.args[1]


def test_tampered_signature_raises():
    sess = 'sess12345'
    token = make_token(sess, nonce='abc')
    timestamp, nonce, sig = token.split('.', 2)
    tampered = f'{timestamp}.{nonce}.{("0" * len(sig))}'
    with pytest.raises(HTTPException) as exc:
        validate_token(tampered, sess)
    assert 'invalid signature' in exc.value.args[1]


def test_make_token_format():
    token = make_token('sess', nonce='nonce123', timestamp_ms=1700000000000)
    parts = token.split('.')
    assert len(parts) == 3
    assert parts[0] == '1700000000000'
    assert parts[1] == 'nonce123'
    expected = hmac.new(b'sess', b'1700000000000:nonce123', hashlib.sha256).hexdigest()
    assert parts[2] == expected


def test_validate_token_returns_nonce_and_timestamp():
    sess = 'sess12345'
    ts = int(time() * 1000)
    token = make_token(sess, nonce='abc123', timestamp_ms=ts)
    nonce, returned_ts = validate_token(token, sess)
    assert nonce == 'abc123'
    assert returned_ts == ts


def test_invalid_nonce_charset_rejected():
    sess = 'sess12345'
    token = make_token(sess, nonce='bad nonce!')
    with pytest.raises(HTTPException) as exc:
        validate_token(token, sess)
    assert 'invalid nonce' in exc.value.args[1]


def test_invalid_nonce_too_long():
    sess = 'sess12345'
    token = make_token(sess, nonce='a' * 65)
    with pytest.raises(HTTPException) as exc:
        validate_token(token, sess)
    assert 'invalid nonce' in exc.value.args[1]


@pytest.mark.asyncio
async def test_consume_nonce_first_use_succeeds(fake_redis):
    await consume_nonce('sess12345', 'nonce-abc', int(time() * 1000))


@pytest.mark.asyncio
async def test_consume_nonce_replay_blocked(fake_redis):
    sess = 'sess12345'
    nonce = 'nonce-replay'
    ts = int(time() * 1000)
    await consume_nonce(sess, nonce, ts)
    with pytest.raises(HTTPException) as exc:
        await consume_nonce(sess, nonce, ts)
    assert exc.value.args[0] == 403
    assert 'replayed' in exc.value.args[1]


@pytest.mark.asyncio
async def test_consume_nonce_isolated_per_session(fake_redis):
    nonce = 'shared-nonce'
    ts = int(time() * 1000)
    await consume_nonce('sessA', nonce, ts)
    # different session_token derives a different namespace — must succeed
    await consume_nonce('sessB', nonce, ts)


@pytest.mark.asyncio
async def test_validate_token_async_replay_blocked(fake_redis):
    sess = 'sess12345'
    token = make_token(sess, nonce='replay-nonce')
    await validate_token_async(token, sess)
    with pytest.raises(HTTPException) as exc:
        await validate_token_async(token, sess)
    assert 'replayed' in exc.value.args[1]


@pytest.mark.asyncio
async def test_validate_token_async_distinct_nonces_succeed(fake_redis):
    sess = 'sess12345'
    await validate_token_async(make_token(sess, nonce='n1'), sess)
    await validate_token_async(make_token(sess, nonce='n2'), sess)
