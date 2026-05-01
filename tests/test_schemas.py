"""Tests for the optional Pydantic schema integration."""
import pytest
from pydantic import BaseModel, Field

from zeromcp.exception import HTTPException
from zeromcp.schemas import (
    is_pydantic_model,
    openapi,
    serialize_output,
    validate_input,
)


class CreateUser(BaseModel):
    email: str
    name: str = Field(min_length=1)


class UserOut(BaseModel):
    id: int
    email: str


def test_is_pydantic_model_true():
    assert is_pydantic_model(CreateUser) is True


def test_is_pydantic_model_false_for_none():
    assert is_pydantic_model(None) is False


def test_is_pydantic_model_false_for_plain_class():
    class Plain:
        pass
    assert is_pydantic_model(Plain) is False


def test_validate_input_ok():
    out = validate_input(CreateUser, {'email': 'a@b.com', 'name': 'A'})
    assert out == {'email': 'a@b.com', 'name': 'A'}


def test_validate_input_missing_field_raises_422():
    with pytest.raises(HTTPException) as exc:
        validate_input(CreateUser, {'email': 'a@b.com'})
    assert exc.value.args[0] == 422
    errors = exc.value.args[1]
    assert any(e['field'] == 'name' for e in errors)


def test_validate_input_invalid_type_raises_422():
    with pytest.raises(HTTPException) as exc:
        validate_input(CreateUser, {'email': 'a@b.com', 'name': ''})
    assert exc.value.args[0] == 422


def test_serialize_output_dict():
    out = serialize_output(UserOut, {'id': 1, 'email': 'a@b.com', 'extra': 'drop'})
    assert out == {'id': 1, 'email': 'a@b.com'}


def test_serialize_output_list():
    out = serialize_output(UserOut, [
        {'id': 1, 'email': 'a@b.com'},
        {'id': 2, 'email': 'c@d.com'},
    ])
    assert len(out) == 2
    assert out[0]['id'] == 1


def test_openapi_decorator_attaches_metadata():
    @openapi(summary='List things', request=CreateUser, response=UserOut, tags=['x'])
    async def handler(self, request, match=None):
        pass

    meta = handler.__openapi__
    assert meta['summary'] == 'List things'
    assert meta['request'] is CreateUser
    assert meta['response'] is UserOut
    assert meta['tags'] == ['x']
