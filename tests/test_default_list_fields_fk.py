"""Default ``list_fields`` includes the ``*_id`` column for forward FKs.

Regression: an earlier refactor skipped FKs in the auto-population branch,
so resources without an explicit ``list_fields`` lost ``account_id`` /
``owner_id`` from list responses. The default LIST should round-trip FK
references like the user's existing systems expect."""
from zeromcp.base import BaseResource
from tests.testapp.models import User, Db


class UserResource(BaseResource):
    model = User
    authenticated = False


class DbResource(BaseResource):
    model = Db
    authenticated = False


def test_default_list_fields_includes_fk_attname():
    res = UserResource()
    assert 'account_id' in res.list_fields
    assert 'account' not in res.list_fields  # not the relation name
    # plain fields still present
    assert 'email' in res.list_fields
    assert 'password' in res.list_fields
    # PK present
    assert 'id' in res.list_fields


def test_default_list_fields_includes_one_to_one_attname():
    res = DbResource()
    assert 'account_id' in res.list_fields


def test_explicit_list_fields_override_still_wins():
    class Slim(BaseResource):
        model = User
        authenticated = False
        list_fields = ['id', 'email']

    res = Slim()
    assert res.list_fields == ['id', 'email']
