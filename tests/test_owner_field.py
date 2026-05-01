import pytest

from zeromcp.base import BaseResource
from zeromcp.exception import HTTPException
from tests.testapp.models import Space


class NoOwnerResource(BaseResource):
    model = Space


class OwnedResource(BaseResource):
    model = Space
    owner_field = 'owner_id'


def test_no_owner_field_no_filter():
    res = NoOwnerResource()
    assert res._ownership_filter() == {}


def test_owner_field_requires_authenticated_user():
    res = OwnedResource()
    res.user = None
    with pytest.raises(HTTPException) as exc:
        res._ownership_filter()
    assert exc.value.args[0] == 401


def test_owner_field_requires_user_id():
    res = OwnedResource()
    res.user = {}
    with pytest.raises(HTTPException) as exc:
        res._ownership_filter()
    assert exc.value.args[0] == 401


def test_owner_field_with_user_returns_filter():
    res = OwnedResource()
    res.user = {'id': 42}
    assert res._ownership_filter() == {'owner_id': 42}
