import re

SESSION_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_\-:]{5,100}$')


def validate_session_key(session_key):
    if not session_key or not SESSION_KEY_PATTERN.match(session_key):
        return None
    return session_key


def make_list(data):
    if not data:
        data = []
    elif not isinstance(data, list):
        data = [data]

    return data


def make_unique_list(data):
    return list(set(make_list(data)))


def normalize_field(name):
    # Used by Filter.distinct to render unique column values as filter
    # options in the UI. Falsy non-int values collapse to 'Blank' so the
    # user sees a single "empty" option for fields where null=True,
    # blank=True both occur (e.g. segment filters).
    #
    # `bool` is a subclass of `int` in Python, so `isinstance(False, int)`
    # would be True. We use `type(name) is int` instead to keep False in
    # the 'Blank' bucket alongside '' and [].
    #
    # Behaviour table:
    #
    # | Input  | Output    | Notes                                   |
    # |--------|-----------|-----------------------------------------|
    # | None   | 'Null'    |                                         |
    # | 0      | 0         | int passes through                      |
    # | 0.0    | 'Blank'   | float, falsy, not int                   |
    # | ''     | 'Blank'   |                                         |
    # | 'x'    | 'x'       |                                         |
    # | []     | 'Blank'   | empty collection, falsy                 |
    # | True   | True      | int(True) == 1, truthy                  |
    # | False  | 'Blank'   | bool not in int bucket here, falsy      |
    #
    # If the segment UI ever needs False to be a real value (not "empty"),
    # swap `type(name) is not int` for `not isinstance(name, int)`.
    if name is None:
        return 'Null'
    if type(name) is not int and not name:
        return 'Blank'
    return name
