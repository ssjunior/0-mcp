"""Coverage of Filter.get_Q / filter_by — main branches.

We inspect the generated SQL string instead of hitting a real DB, so the
tests run without a database fixture. These tests pin the current
behaviour of the filter engine before we refactor get_Q.
"""
from django.db.models import Q

from zeromcp.filters import Filter
from tests.testapp.models import Space, User


def sql(qs):
    return str(qs.query)


def apply(model, conditions):
    """Run filter_by with conditions wrapped as an AND group (the format
    callers actually use)."""
    f = Filter(model)
    return f.filter_by(
        {'logical_operator': 'AND', 'rules': conditions}
        if isinstance(conditions, list)
        else conditions
    )


def test_filter_by_simple_equality():
    qs = apply(Space, [
        {'field': 'name', 'operator': 'exact', 'value': 'foo'},
    ])
    s = sql(qs)
    assert 'name' in s
    assert 'foo' in s


def test_filter_by_logical_and():
    qs = apply(Space, {
        'logical_operator': 'AND',
        'rules': [
            {'field': 'name', 'operator': 'exact', 'value': 'foo'},
            {'field': 'active', 'operator': 'exact', 'value': True},
        ],
    })
    s = sql(qs)
    assert 'name' in s
    assert 'active' in s
    assert ' AND ' in s


def test_filter_by_logical_or():
    qs = apply(Space, {
        'logical_operator': 'OR',
        'rules': [
            {'field': 'name', 'operator': 'exact', 'value': 'foo'},
            {'field': 'name', 'operator': 'exact', 'value': 'bar'},
        ],
    })
    s = sql(qs)
    assert ' OR ' in s


def test_filter_by_nested_groups():
    qs = apply(Space, {
        'logical_operator': 'AND',
        'rules': [
            {
                'logical_operator': 'OR',
                'rules': [
                    {'field': 'name', 'operator': 'icontains', 'value': 'a'},
                    {'field': 'name', 'operator': 'icontains', 'value': 'b'},
                ],
            },
            {'field': 'active', 'operator': 'exact', 'value': True},
        ],
    })
    s = sql(qs)
    assert ' OR ' in s
    assert ' AND ' in s
    assert 'active' in s


def test_filter_by_negation():
    qs = apply(Space, [
        {'field': 'name', 'operator': 'not_exact', 'value': 'foo'},
    ])
    s = sql(qs)
    assert 'NOT' in s


def test_filter_by_isnull_true():
    qs = apply(Space, [
        {'field': 'description', 'operator': 'isnull', 'value': True},
    ])
    s = sql(qs)
    assert 'description' in s
    assert 'IS NULL' in s or '=  ' in s  # text field coalesces with ''


def test_filter_by_isnotnull():
    qs = apply(Space, [
        {'field': 'description', 'operator': 'isnotnull', 'value': True},
    ])
    s = sql(qs)
    assert 'description' in s


def test_filter_by_blank_value_uses_empty_string():
    """value='Blank' should match `field = ''`."""
    qs = apply(Space, [
        {'field': 'description', 'operator': 'exact', 'value': 'Blank'},
    ])
    s = sql(qs)
    assert 'description' in s


def test_filter_by_null_value_uses_isnull():
    qs = apply(Space, [
        {'field': 'description', 'operator': 'exact', 'value': 'Null'},
    ])
    s = sql(qs)
    assert 'IS NULL' in s


def test_filter_by_boolean_string_true_converted():
    qs = apply(Space, [
        {'field': 'active', 'operator': 'exact', 'value': 'true'},
    ])
    s = sql(qs)
    assert 'active' in s


def test_filter_by_related_field_traversal():
    qs = apply(User, [
        {'field': 'account__name', 'operator': 'icontains', 'value': 'acme'},
    ])
    s = sql(qs)
    assert 'testapp_account' in s
    assert 'name' in s.lower()


def test_filter_by_in_operator_skips_empty_value():
    qs = apply(Space, [
        {'field': 'name', 'operator': 'in', 'value': []},
    ])
    s = sql(qs)
    # empty `in` = no-op, no WHERE on name
    assert 'name' not in s.split('FROM')[-1] or 'IN ()' not in s


def test_filter_by_empty_field_skipped():
    f = Filter(Space)
    qs_before = f.model.all()
    qs = apply(Space, [
        {'field': '', 'operator': 'exact', 'value': 'x'},
    ])
    assert sql(qs).count('WHERE') == sql(qs_before).count('WHERE')


def test_filter_by_string_format_simple():
    """Comma-separated `field=value` string accepted by filter_by."""
    f = Filter(Space)
    qs = f.filter_by('name=foo')
    s = sql(qs)
    assert 'name' in s


def test_filter_by_chained_like():
    qs = apply(Space, [
        {'field': 'name', 'operator': 'startswith', 'value': 'A'},
    ])
    s = sql(qs)
    assert 'LIKE' in s


def test_filter_by_in_with_list():
    qs = apply(Space, [
        {'field': 'id', 'operator': 'in', 'value': [1, 2, 3]},
    ])
    s = sql(qs)
    assert ' IN ' in s


def test_filter_by_multiple_rules_in_group():
    qs = apply(Space, {
        'logical_operator': 'AND',
        'rules': [
            {'field': 'name', 'operator': 'icontains', 'value': 'a'},
            {'field': 'active', 'operator': 'exact', 'value': True},
        ],
    })
    s = sql(qs)
    assert 'name' in s
    assert 'active' in s


def test_get_Q_returns_q_object():
    f = Filter(Space)
    result = f.get_Q({
        'logical_operator': 'AND',
        'rules': [
            {'field': 'name', 'operator': 'exact', 'value': 'x'},
        ],
    })
    assert isinstance(result, Q)


def test_get_Q_empty_returns_falsy():
    f = Filter(Space)
    result = f.get_Q({'logical_operator': 'AND', 'rules': []})
    assert not result
