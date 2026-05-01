from zeromcp.util import (
    make_list,
    make_unique_list,
    normalize_field,
    validate_session_key,
)


class TestValidateSessionKey:
    def test_valid_alphanumeric(self):
        assert validate_session_key('abc12345') == 'abc12345'

    def test_valid_with_dash_underscore_colon(self):
        assert validate_session_key('1:2:abc-def_xyz') == '1:2:abc-def_xyz'

    def test_too_short(self):
        assert validate_session_key('abc') is None

    def test_too_long(self):
        assert validate_session_key('a' * 101) is None

    def test_invalid_chars(self):
        assert validate_session_key('abc.def') is None
        assert validate_session_key('abc/def') is None
        assert validate_session_key('abc=def') is None

    def test_none(self):
        assert validate_session_key(None) is None

    def test_empty(self):
        assert validate_session_key('') is None


class TestMakeList:
    def test_none(self):
        assert make_list(None) == []

    def test_empty_list(self):
        assert make_list([]) == []

    def test_scalar(self):
        assert make_list('x') == ['x']

    def test_list_passthrough(self):
        assert make_list(['a', 'b']) == ['a', 'b']


class TestMakeUniqueList:
    def test_dedupe(self):
        assert sorted(make_unique_list(['a', 'b', 'a'])) == ['a', 'b']


class TestNormalizeField:
    def test_none(self):
        assert normalize_field(None) == 'Null'

    def test_empty_string(self):
        assert normalize_field('') == 'Blank'

    def test_zero_int_passthrough(self):
        assert normalize_field(0) == 0

    def test_value(self):
        assert normalize_field('x') == 'x'
