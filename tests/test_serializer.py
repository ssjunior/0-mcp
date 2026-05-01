import json
from datetime import datetime
from decimal import Decimal
import zoneinfo

from zeromcp.serializer import CustomJSONEncoder


def test_datetime_formatted_with_timezone():
    encoder = CustomJSONEncoder.with_timezone(zoneinfo.ZoneInfo('UTC'))
    out = json.dumps(
        {'at': datetime(2024, 1, 15, 12, 30, 45, tzinfo=zoneinfo.ZoneInfo('UTC'))},
        cls=encoder,
    )
    assert '2024-01-15 12:30:45' in out


def test_decimal_passthrough():
    encoder = CustomJSONEncoder.with_timezone(zoneinfo.ZoneInfo('UTC'))
    out = json.dumps({'v': Decimal('1.50')}, cls=encoder)
    assert '1.50' in out


def test_datetime_converted_to_target_tz():
    encoder = CustomJSONEncoder.with_timezone(zoneinfo.ZoneInfo('America/Sao_Paulo'))
    out = json.dumps(
        {'at': datetime(2024, 1, 15, 12, 0, 0, tzinfo=zoneinfo.ZoneInfo('UTC'))},
        cls=encoder,
    )
    assert '2024-01-15 09:00:00' in out


def test_with_timezone_does_not_mutate_base_class():
    e1 = CustomJSONEncoder.with_timezone(zoneinfo.ZoneInfo('UTC'))
    e2 = CustomJSONEncoder.with_timezone(zoneinfo.ZoneInfo('America/Sao_Paulo'))
    assert e1 is not e2
    assert e1 is not CustomJSONEncoder
    dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=zoneinfo.ZoneInfo('UTC'))
    assert '12:00:00' in json.dumps({'at': dt}, cls=e1)
    assert '09:00:00' in json.dumps({'at': dt}, cls=e2)
    assert CustomJSONEncoder.timezone == 'UTC'
