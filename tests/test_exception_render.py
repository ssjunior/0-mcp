"""HTTPException.render must log 5xx errors so Sentry / log aggregators
see them. Silent 5xx renders were the root of an opaque-error incident
in production."""
import json
import logging

import pytest

from zeromcp.exception import HTTPException


def _render(status, detail='boom'):
    exc = HTTPException(status, detail)
    response = exc.render(exc)
    body = json.loads(response.content.decode('utf-8'))
    return response, body


def test_500_logs_exception(caplog):
    with caplog.at_level(logging.ERROR, logger='zeromcp.exception'):
        response, body = _render(500, 'database unreachable')

    assert response.status_code == 500
    assert body == {'success': False, 'status': 500, 'detail': 'database unreachable'}

    records = [r for r in caplog.records if r.name == 'zeromcp.exception']
    assert records, 'expected a log record for 500 render'
    record = records[0]
    assert record.levelno == logging.ERROR
    assert '500' in record.getMessage()
    assert 'database unreachable' in record.getMessage()
    # exc_info attached so traceback is preserved for Sentry-style sinks
    assert record.exc_info is not None


def test_503_also_logs(caplog):
    with caplog.at_level(logging.ERROR, logger='zeromcp.exception'):
        _render(503, 'upstream down')
    records = [r for r in caplog.records if r.name == 'zeromcp.exception']
    assert records, '5xx other than 500 must also log'


def test_400_does_not_log(caplog):
    """4xx is client-side; no log noise — they're routine."""
    with caplog.at_level(logging.DEBUG, logger='zeromcp.exception'):
        response, body = _render(400, 'bad input')
    assert response.status_code == 400
    records = [r for r in caplog.records if r.name == 'zeromcp.exception']
    assert not records


def test_403_does_not_log(caplog):
    with caplog.at_level(logging.DEBUG, logger='zeromcp.exception'):
        _render(403, 'forbidden')
    records = [r for r in caplog.records if r.name == 'zeromcp.exception']
    assert not records


def test_404_does_not_log(caplog):
    with caplog.at_level(logging.DEBUG, logger='zeromcp.exception'):
        _render(404, 'not found')
    records = [r for r in caplog.records if r.name == 'zeromcp.exception']
    assert not records


def test_response_body_unchanged_for_5xx():
    """Logging is additive — must not change the wire response."""
    response, body = _render(500, 'something')
    assert body == {'success': False, 'status': 500, 'detail': 'something'}
