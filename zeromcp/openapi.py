"""OpenAPI 3.0.3 spec generation from `endpoints` registered via `get_routes`.

For each resource:
- If `create_schema`/`update_schema`/`list_schema` is set, generate paths
  and request/response bodies from those Pydantic models.
- Otherwise, fall back to introspecting the Django model's fields.

Custom routes declared in `routes` are included; metadata may be attached
via the `@openapi(...)` decorator on the handler.
"""
import logging
import re

from django.db.models.fields import NOT_PROVIDED

from .schemas import is_pydantic_model, PYDANTIC_AVAILABLE

from .settings_helper import get_cookie_id

COOKIE_ID = get_cookie_id()

logger = logging.getLogger(__name__)


_DJANGO_TO_OPENAPI = {
    'AutoField': {'type': 'integer'},
    'BigAutoField': {'type': 'integer'},
    'BigIntegerField': {'type': 'integer'},
    'BooleanField': {'type': 'boolean'},
    'CharField': {'type': 'string'},
    'DateField': {'type': 'string', 'format': 'date'},
    'DateTimeField': {'type': 'string', 'format': 'date-time'},
    'DecimalField': {'type': 'number'},
    'EmailField': {'type': 'string', 'format': 'email'},
    'FloatField': {'type': 'number'},
    'IntegerField': {'type': 'integer'},
    'JSONField': {'type': 'object'},
    'PositiveIntegerField': {'type': 'integer'},
    'SlugField': {'type': 'string'},
    'SmallIntegerField': {'type': 'integer'},
    'TextField': {'type': 'string'},
    'TimeField': {'type': 'string', 'format': 'time'},
    'UUIDField': {'type': 'string', 'format': 'uuid'},
    'URLField': {'type': 'string', 'format': 'uri'},
    'ForeignKey': {'type': 'integer'},
    'OneToOneField': {'type': 'integer'},
}


def _field_to_openapi(field):
    schema = dict(_DJANGO_TO_OPENAPI.get(field.get_internal_type(), {'type': 'string'}))
    if field.has_default() and field.default is not NOT_PROVIDED:
        try:
            value = field.default() if callable(field.default) else field.default
            # Only include defaults that are JSON-encodable scalars/containers.
            # Auto-generated values (uuid4, datetime.now, …) get coerced to
            # str so the spec stays serializable; callers know the value is
            # server-assigned anyway.
            import json as _json
            try:
                _json.dumps(value)
                schema['default'] = value
            except (TypeError, ValueError):
                schema['default'] = str(value)
        except Exception:
            pass
    if field.null:
        schema['nullable'] = True
    if getattr(field, 'max_length', None):
        schema['maxLength'] = field.max_length
    verbose = getattr(field, 'verbose_name', None)
    if verbose and str(verbose) != field.name.replace('_', ' '):
        # Skip Django's auto-generated verbose_name (just the field name with
        # underscores → spaces); only surface human-written labels.
        schema['title'] = str(verbose)
    if field.help_text:
        schema['description'] = str(field.help_text)
    if getattr(field, 'choices', None):
        # Build enum + x-choices map of value → label so the agent can
        # render the meaning of each option. Keep the schema's type
        # consistent with the enum's value type (integer choices stay
        # integers, not strings).
        values = [c[0] for c in field.choices]
        schema['enum'] = values
        schema['x-choices'] = {str(c[0]): str(c[1]) for c in field.choices}
        if values and all(isinstance(v, int) for v in values):
            schema['type'] = 'integer'
    return schema


def _model_introspect_schema(model, fields=None):
    if not model:
        return {'type': 'object'}
    properties = {}
    required = []
    for field in model._meta.local_fields:
        if fields and field.name not in fields:
            continue
        properties[field.name] = _field_to_openapi(field)
        if not field.blank and not field.null and not field.has_default() and not field.primary_key:
            required.append(field.name)
    schema = {'type': 'object', 'properties': properties}
    if required:
        schema['required'] = required
    return schema


def _pydantic_schema(model_cls, components):
    """Return a $ref dict pointing to the schema in `components.schemas`,
    after registering the schema (and any nested defs)."""
    name = model_cls.__name__
    full = model_cls.model_json_schema(ref_template='#/components/schemas/{model}')
    defs = full.pop('$defs', {})
    components.setdefault('schemas', {})
    components['schemas'][name] = full
    for def_name, def_schema in defs.items():
        components['schemas'].setdefault(def_name, def_schema)
    return {'$ref': f'#/components/schemas/{name}'}


def _route_path(route_pattern):
    """Convert the resource regex into an OpenAPI path. Strip trailing `(.*)$`
    catch-all and prefix with `/`."""
    path = route_pattern.rstrip('$')
    path = re.sub(r'\(\.\*\)$', '', path)
    if not path.startswith('/'):
        path = '/' + path
    return path


def _resource_tag(view_cls) -> str:
    """Pick a clean OpenAPI tag for a resource.

    Priority:

    1. Explicit ``tag`` attribute on the resource (escape hatch).
    2. The bound model's class name — what most users actually mean.
    3. The resource class name minus the trailing ``Resource``
       (``ClientResource`` → ``Client``).
    4. The class name as-is, last resort.
    """
    explicit = getattr(view_cls, 'tag', None)
    if explicit:
        return str(explicit)
    model = getattr(view_cls, 'model', None)
    if model is not None:
        return model.__name__
    name = view_cls.__name__
    if name.endswith('Resource') and len(name) > len('Resource'):
        return name[:-len('Resource')]
    return name


def _resource_paths(route_pattern, view_cls, components):
    base_path = _route_path(route_pattern)
    detail_path = base_path.rstrip('/') + '/{id}'
    methods = view_cls.allowed_methods or []
    paths = {}

    tag = _resource_tag(view_cls)
    summary_prefix = getattr(view_cls, 'summary', None) or tag
    resource_description = getattr(view_cls, 'description', None)
    list_response = _build_response_schema(view_cls, components, kind='list')
    detail_response = _build_response_schema(view_cls, components, kind='detail')

    if 'get' in methods:
        paths.setdefault(base_path, {})['get'] = {
            'tags': [tag],
            'summary': f'List {summary_prefix}',
            'parameters': _list_parameters(view_cls),
            'responses': {'200': {'description': 'OK', 'content': {
                'application/json': {'schema': {
                    'type': 'object',
                    'properties': {
                        'meta': {'type': 'object'},
                        'objects': {'type': 'array', 'items': list_response},
                    },
                }}
            }}},
        }
        paths.setdefault(detail_path, {})['get'] = {
            'tags': [tag],
            'summary': f'Get {summary_prefix}',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'OK', 'content': {
                'application/json': {'schema': detail_response}
            }}},
        }

    if 'post' in methods:
        body = _build_body_schema(view_cls, components, kind='create')
        paths.setdefault(base_path, {})['post'] = {
            'tags': [tag],
            'summary': f'Create {summary_prefix}',
            'requestBody': {'required': True, 'content': {
                'application/json': {'schema': body}
            }},
            'responses': {'200': {'description': 'Created', 'content': {
                'application/json': {'schema': detail_response}
            }}},
        }

    if 'patch' in methods:
        body = _build_body_schema(view_cls, components, kind='update')
        paths.setdefault(detail_path, {})['patch'] = {
            'tags': [tag],
            'summary': f'Update {summary_prefix}',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'}}],
            'requestBody': {'required': True, 'content': {
                'application/json': {'schema': body}
            }},
            'responses': {'200': {'description': 'Updated', 'content': {
                'application/json': {'schema': detail_response}
            }}},
        }

    if 'delete' in methods:
        paths.setdefault(detail_path, {})['delete'] = {
            'tags': [tag],
            'summary': f'Delete {summary_prefix}',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'Deleted'}},
        }

    for route in (view_cls.routes or []):
        try:
            inst = view_cls()
            handler = getattr(inst, route['func'], None)
        except Exception:
            handler = None
        meta = getattr(handler, '__openapi__', None) if handler else None
        custom_path = base_path.rstrip('/') + '/' + route['path'].lstrip('/').rstrip('$')
        custom_path = re.sub(r'\([^)]*\)', '{param}', custom_path)
        for method in (route.get('allowed_methods') or methods):
            op = {
                'tags': [tag],
                'summary': (meta or {}).get('summary') or route['func'],
                'responses': {'200': {'description': 'OK'}},
            }
            if meta:
                if meta.get('description'):
                    op['description'] = meta['description']
                if meta.get('request') and is_pydantic_model(meta['request']):
                    op['requestBody'] = {'required': True, 'content': {
                        'application/json': {'schema': _pydantic_schema(meta['request'], components)}
                    }}
                if meta.get('response') and is_pydantic_model(meta['response']):
                    op['responses']['200']['content'] = {
                        'application/json': {'schema': _pydantic_schema(meta['response'], components)}
                    }
                if meta.get('tags'):
                    op['tags'] = meta['tags']
            paths.setdefault(custom_path, {})[method.lower()] = op

    return paths


def _build_body_schema(view_cls, components, kind):
    schema_attr = {'create': 'create_schema', 'update': 'update_schema'}[kind]
    fields_attr = {'create': 'create_fields', 'update': 'update_fields'}[kind]
    schema_cls = getattr(view_cls, schema_attr, None)
    if is_pydantic_model(schema_cls):
        return _pydantic_schema(schema_cls, components)
    fields = getattr(view_cls, fields_attr, None)
    return _model_introspect_schema(view_cls.model, fields)


def _build_response_schema(view_cls, components, kind):
    if kind == 'list':
        schema_cls = getattr(view_cls, 'list_schema', None)
        fields = getattr(view_cls, 'list_fields', None)
    else:
        schema_cls = getattr(view_cls, 'list_schema', None) or getattr(view_cls, 'create_schema', None)
        fields = getattr(view_cls, 'edit_fields', None) or getattr(view_cls, 'list_fields', None)
    if is_pydantic_model(schema_cls):
        return _pydantic_schema(schema_cls, components)
    return _model_introspect_schema(view_cls.model, fields)


def _list_parameters(view_cls):
    params = [
        {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'default': 1}},
        {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'default': 25}},
        {'name': 'order_by', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'search', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'fields', 'in': 'query', 'schema': {'type': 'string'},
         'description': 'Comma-separated subset of list_fields'},
        {'name': 'filter', 'in': 'query', 'schema': {'type': 'string'},
         'description': 'JSON-encoded filter expression'},
    ]
    return params


def build_spec(endpoints, title='0-mcp', version=None, description=None):
    if version is None:
        from . import __version__ as version
    components = {'schemas': {}, 'securitySchemes': {
        'cookieAuth': {'type': 'apiKey', 'in': 'cookie', 'name': COOKIE_ID},
        'apiKeyAuth': {'type': 'apiKey', 'in': 'header', 'name': 'X-Api-Key'},
        'bearerAuth': {'type': 'http', 'scheme': 'bearer'},
    }}
    paths = {}
    tags = []
    seen_tags = set()
    for route_pattern, view_cls in endpoints.items():
        try:
            for path, ops in _resource_paths(route_pattern, view_cls, components).items():
                paths.setdefault(path, {}).update(ops)
            tag_name = _resource_tag(view_cls)
            if tag_name not in seen_tags:
                seen_tags.add(tag_name)
                tag_entry = {'name': tag_name}
                if getattr(view_cls, 'description', None):
                    tag_entry['description'] = view_cls.description
                tags.append(tag_entry)
        except Exception:
            logger.warning(
                'openapi: skipping resource %s (%s)',
                getattr(view_cls, '__name__', view_cls),
                route_pattern,
                exc_info=True,
            )
            continue

    spec = {
        'openapi': '3.0.3',
        'info': {'title': title, 'version': version},
        'paths': paths,
        'components': components,
        'security': [
            {'cookieAuth': []}, {'apiKeyAuth': []}, {'bearerAuth': []},
        ],
        'tags': tags,
    }
    if description:
        spec['info']['description'] = description
    if not PYDANTIC_AVAILABLE:
        spec['info'].setdefault('description', '')
        spec['info']['description'] += ' (pydantic not installed — schemas inferred from Django models only)'
    return spec


SCALAR_HTML = """<!doctype html>
<html><head>
<title>API docs</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<script
  id="api-reference"
  data-url="__SPEC_URL__"
  data-configuration='{"agent":{"disabled":true}}'></script>
<script>
  const el = document.getElementById('api-reference');
  const here = window.location.pathname.replace(/\\/docs\\/?$/, '');
  el.dataset.url = here + '/openapi.json';
</script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body></html>
"""

# Backwards-compat alias.
SWAGGER_HTML = SCALAR_HTML
