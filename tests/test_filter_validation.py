import pytest

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


class SpaceResource(BaseResource):
    model = Space
    filter_fields = ['name', 'active', 'account']


class NoFilterResource(BaseResource):
    model = Space


def test_validate_no_filter_fields_raises():
    res = NoFilterResource()
    with pytest.raises(HTTPException) as exc:
        res._validate_filter_fields([{'field': 'name', 'value': 'x'}])
    assert exc.value.args[0] == 403


def test_validate_allowed_field():
    res = SpaceResource()
    res._validate_filter_fields([{'field': 'name', 'value': 'x'}])


def test_validate_disallowed_field():
    res = SpaceResource()
    with pytest.raises(HTTPException) as exc:
        res._validate_filter_fields([{'field': 'secret', 'value': 'x'}])
    assert exc.value.args[0] == 403
    assert 'secret' in exc.value.args[1]


def test_validate_nested_field_root_match():
    res = SpaceResource()
    res._validate_filter_fields([{'field': 'account__name', 'value': 'x'}])


def test_validate_nested_field_root_disallowed():
    res = SpaceResource()
    with pytest.raises(HTTPException):
        res._validate_filter_fields([{'field': 'secret__hash', 'value': 'x'}])


def test_validate_logical_operator_recurses():
    res = SpaceResource()
    res._validate_filter_fields({
        'logical_operator': 'AND',
        'rules': [
            {'field': 'name', 'value': 'x'},
            {'field': 'active', 'value': True},
        ],
    })


def test_validate_logical_operator_blocks_inner_disallowed():
    res = SpaceResource()
    with pytest.raises(HTTPException):
        res._validate_filter_fields({
            'logical_operator': 'AND',
            'rules': [
                {'field': 'name', 'value': 'x'},
                {'field': 'secret', 'value': 'y'},
            ],
        })


def test_validate_custom_attributes_allowed():
    res = SpaceResource()
    res._validate_filter_fields([{'field': 'custom_attributes__color', 'value': 'red'}])


def test_validate_generated_creation_date_allowed():
    res = SpaceResource()
    res._validate_filter_fields([{'field': 'generated_creation_date', 'value': '2024-01-01'}])


def test_validate_empty_field_skipped():
    res = SpaceResource()
    res._validate_filter_fields([{'field': '', 'value': 'x'}])
    res._validate_filter_fields([{'value': 'x'}])
