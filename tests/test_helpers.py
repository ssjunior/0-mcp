from zeromcp.helpers import (
    LOCAL_HOST,
    get_tz,
    make_list,
    re_id,
    search_regex,
)


def test_get_tz_caches():
    a = get_tz('UTC')
    b = get_tz('UTC')
    assert a is b


def test_get_tz_different():
    a = get_tz('UTC')
    b = get_tz('America/Sao_Paulo')
    assert a is not b


def test_make_list_helpers():
    assert make_list(None) == []
    assert make_list('x') == ['x']
    assert make_list(['a']) == ['a']


def test_re_id_int():
    m = re_id.match('/users/42')
    assert m
    assert m.group('int_id') == '42'
    assert m.group('uuid') is None


def test_re_id_uuid():
    m = re_id.match('/users/abcd1234-abcd-abcd-abcd-abcdef123456')
    assert m
    assert m.group('uuid') == 'abcd1234-abcd-abcd-abcd-abcdef123456'


def test_re_id_no_match():
    assert re_id.match('/users') is None
    assert re_id.match('/users/me') is None


def test_search_regex():
    assert search_regex.search('field__gte')
    assert search_regex.search('field__startswith')
    assert not search_regex.search('field')
    assert not search_regex.search('field__exact')


def test_local_host():
    assert LOCAL_HOST.match('localhost')
    assert LOCAL_HOST.match('127.0.0.1')
    assert LOCAL_HOST.match('localhost:8000')
    assert not LOCAL_HOST.match('example.com')
