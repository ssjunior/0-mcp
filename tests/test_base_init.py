from zeromcp.base import BaseResource
from tests.testapp.models import Space


class CustomResource(BaseResource):
    model = Space
    list_fields = ['id', 'name']
    filter_fields = ['name']
    update_fields = ['name', 'description']
    create_fields = ['name']
    routes = [{'path': r'foo$', 'func': 'foo'}]


def test_default_lists_become_empty():
    res = BaseResource()
    assert res.fields == []
    assert res.all_fields == []
    assert res.fk_fields == []
    assert res.m2m_fields == []
    assert res.filter_fields == []
    assert res.queryset_filter == {}
    assert res.search_fields == []
    assert res.order_fields == []
    assert res.list_fields == []
    assert res.list_exclude_fields == []
    assert res.edit_fields == []
    assert res.edit_related_fields == {}
    assert res.edit_exclude_fields == ['_state']
    assert res.update_fields == []
    assert res.create_fields == []
    assert res.routes == []
    assert res.filters == []


def test_subclass_lists_normalized():
    res = CustomResource()
    assert res.list_fields == ['id', 'name']
    assert res.filter_fields == ['name']
    assert res.update_fields == ['name', 'description']
    assert res.create_fields == ['name']
    assert res.routes == [{'path': r'foo$', 'func': 'foo'}]


def test_instance_isolation():
    a = BaseResource()
    b = BaseResource()
    a.list_fields.append('x')
    assert b.list_fields == []
    assert BaseResource.list_fields is None


def test_routes_isolation():
    a = BaseResource()
    b = BaseResource()
    a.routes.append({'path': 'x', 'func': 'y'})
    assert b.routes == []


def test_class_attrs_remain_none():
    assert BaseResource.update_fields is None
    assert BaseResource.create_fields is None
    assert BaseResource.routes is None
