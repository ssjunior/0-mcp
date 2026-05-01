from zeromcp.util import normalize_field


def test_none_returns_null():
    assert normalize_field(None) == 'Null'


def test_empty_string_returns_blank():
    assert normalize_field('') == 'Blank'


def test_zero_int_passes_through():
    assert normalize_field(0) == 0


def test_negative_int_passes_through():
    assert normalize_field(-5) == -5


def test_string_passes_through():
    assert normalize_field('value') == 'value'


def test_empty_list_returns_blank():
    assert normalize_field([]) == 'Blank'


def test_zero_float_returns_blank():
    # Floats other than int still go through "Blank" path when falsy.
    # 0.0 is falsy and not an int → 'Blank'.
    assert normalize_field(0.0) == 'Blank'


def test_false_returns_blank():
    # bool inherits from int, but legacy behaviour treats False as Blank
    # (used in segment filters where null=True, blank=True fields look
    # the same to the user). Kept intentionally.
    assert normalize_field(False) == 'Blank'


def test_true_passes_through():
    assert normalize_field(True) is True
