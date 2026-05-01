"""Pydantic v2 integration. Optional — `pip install 0-mcp[schemas]`.

Resources may declare `create_schema`, `update_schema`, `list_schema` to
drive request validation, response shaping and OpenAPI generation.
Resources without schemas keep the legacy field-list behaviour.
"""
from .exception import HTTPException

try:
    from pydantic import BaseModel, ValidationError
    PYDANTIC_AVAILABLE = True
except ImportError:
    BaseModel = None
    ValidationError = None
    PYDANTIC_AVAILABLE = False


def is_pydantic_model(obj):
    if not PYDANTIC_AVAILABLE or obj is None:
        return False
    try:
        return isinstance(obj, type) and issubclass(obj, BaseModel)
    except TypeError:
        return False


def _require_pydantic():
    if not PYDANTIC_AVAILABLE:
        raise RuntimeError(
            'pydantic is not installed. Install with '
            '`pip install 0-mcp[schemas]` to use schemas.'
        )


def validate_input(schema, data):
    """Validate `data` against the Pydantic `schema`. Raises HTTPException
    422 with a list of field errors on failure. Returns the validated dict."""
    _require_pydantic()
    try:
        return schema.model_validate(data).model_dump()
    except ValidationError as exc:
        errors = [
            {'field': '.'.join(str(p) for p in err['loc']), 'message': err['msg']}
            for err in exc.errors()
        ]
        raise HTTPException(422, errors)


def serialize_output(schema, obj):
    """Run `obj` through `schema` to shape the response. Accepts dict or
    list-of-dicts. Returns a plain dict / list (JSON-ready)."""
    _require_pydantic()
    if isinstance(obj, list):
        return [schema.model_validate(item).model_dump() for item in obj]
    return schema.model_validate(obj).model_dump()


def openapi(summary=None, description=None, request=None, response=None, tags=None):
    """Decorator that attaches OpenAPI metadata to custom route methods.

    Usage:
        @openapi(summary='Get current user', response=UserSchema)
        async def me(self, request, match=None): ...
    """
    def decorator(func):
        func.__openapi__ = {
            'summary': summary,
            'description': description,
            'request': request,
            'response': response,
            'tags': tags or [],
        }
        return func
    return decorator
