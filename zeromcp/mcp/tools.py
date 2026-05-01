"""Generate MCP tool definitions from a `get_routes`-style endpoints map.

A tool definition is a dict with ``name``, ``description``, ``inputSchema``
and (optional) ``outputSchema``. The format mirrors what the MCP SDK
expects, so we hand it straight through.
"""
import re

from ..openapi import (
    _build_body_schema,
    _build_response_schema,
    _list_parameters,
    _model_introspect_schema,
)
from ..schemas import is_pydantic_model

_VERB_TEMPLATES = {
    'list':   {'verb_method': 'get',    'expects_id': False, 'has_body': False, 'is_write': False},
    'get':    {'verb_method': 'get',    'expects_id': True,  'has_body': False, 'is_write': False},
    'create': {'verb_method': 'post',   'expects_id': False, 'has_body': True,  'is_write': True},
    'update': {'verb_method': 'patch',  'expects_id': True,  'has_body': True,  'is_write': True},
    'delete': {'verb_method': 'delete', 'expects_id': True,  'has_body': False, 'is_write': True},
}

_VERB_DESCRIPTION = {
    'list':   'List {label}s with optional filters, search, ordering and pagination. Returns a paginated envelope with `meta` and `objects`.',
    'get':    'Get a single {label} by id. Returns the full record.',
    'create': 'Create a new {label}. Body must contain the fields listed in inputSchema; missing required fields raise 400.',
    'update': 'Update an existing {label} by id. Pass only the fields you want to change.',
    'delete': 'Delete a {label} by id. Idempotent on missing rows. Returns an empty object on success.',
}

# Field names always stripped from MCP tool schemas (input + output) so an
# agent never sees credentials/secrets even if they exist on the model. Use
# ``BaseResource.sensitive_fields`` to extend per-resource. The match is
# case-insensitive and exact (no prefix/suffix patterns) — keep it tight to
# avoid hiding legitimate columns by accident.
_DEFAULT_SENSITIVE = frozenset({
    'password', 'password_hash', 'pwd',
    'api_key', 'api_secret',
    'secret', 'secret_key',
    'token', 'access_token', 'refresh_token',
    'private_key', 'session_key',
    'otp', 'otp_secret', 'two_factor_secret',
})


def _scrub_schema(schema, extra_excludes):
    """Drop sensitive fields from a JSON-Schema in place. Recurses into
    ``items`` (arrays of objects) so list outputs are scrubbed too."""
    if not isinstance(schema, dict):
        return schema
    blocked = _DEFAULT_SENSITIVE | {f.lower() for f in (extra_excludes or [])}
    props = schema.get('properties')
    if isinstance(props, dict):
        for name in list(props):
            if name.lower() in blocked:
                props.pop(name, None)
        for value in props.values():
            _scrub_schema(value, extra_excludes)
    if isinstance(schema.get('required'), list):
        schema['required'] = [n for n in schema['required'] if n.lower() not in blocked]
    items = schema.get('items')
    if isinstance(items, dict):
        _scrub_schema(items, extra_excludes)
    return schema


def _resource_label(view_cls):
    """Return the lowercase noun used in tool names. Falls back to a
    snake_case version of the class name (``SpaceResource`` → ``space``)."""
    summary = getattr(view_cls, 'summary', None)
    if summary:
        return _slugify(summary)
    name = view_cls.__name__
    if name.endswith('Resource'):
        name = name[:-len('Resource')]
    return _slugify(name)


def _slugify(value):
    s = re.sub(r'(?<!^)(?=[A-Z])', '_', str(value)).lower()
    s = re.sub(r'[^a-z0-9_]+', '_', s).strip('_')
    return s or 'resource'


def _list_input_schema(view_cls):
    """Build the inputSchema for a list tool — search, ordering,
    pagination, plus every field in `filter_fields`."""
    properties = {}
    for p in _list_parameters(view_cls):
        if p.get('in') == 'query':
            properties[p['name']] = p['schema']
    filter_fields = getattr(view_cls, 'filter_fields', None) or []
    model_fields = {}
    if getattr(view_cls, 'model', None):
        try:
            model_fields = {f.name: f for f in view_cls.model._meta.local_fields}
        except Exception:
            model_fields = {}
    for field in filter_fields:
        if field in properties:
            continue
        schema = _filter_field_schema(field, model_fields.get(field))
        properties[field] = schema
    return {'type': 'object', 'properties': properties, 'additionalProperties': True}


def _filter_field_schema(name, model_field):
    """Schema entry for a `filter_fields` query parameter. Pulls type
    and label from the Django model when possible so the agent knows
    what each filter accepts."""
    if model_field is None:
        return {'type': 'string', 'description': f'Filter by {name}.'}
    base = dict(_field_schema_for(model_field))
    verbose = getattr(model_field, 'verbose_name', None)
    label = str(verbose) if verbose and str(verbose) != name.replace('_', ' ') else name
    base['description'] = f'Filter by {label}.'
    base.pop('default', None)
    base.pop('nullable', None)
    base.pop('maxLength', None)
    return base


def _field_schema_for(model_field):
    """Best-effort: reuse the OpenAPI introspection so types match REST."""
    from ..openapi import _field_to_openapi
    try:
        return _field_to_openapi(model_field)
    except Exception:
        return {'type': 'string'}


def _detail_input_schema():
    return {
        'type': 'object',
        'properties': {'id': {'type': 'string', 'description': 'Row id.'}},
        'required': ['id'],
    }


def _write_input_schema(view_cls, kind, components):
    body_schema = _build_body_schema(view_cls, components, kind=kind)
    if kind == 'update':
        merged = {
            'type': 'object',
            'properties': {'id': {'type': 'string', 'description': 'Row id.'}},
            'required': ['id'],
            'allOf': [body_schema],
        }
        return merged
    return body_schema


def _delete_input_schema():
    return _detail_input_schema()


def _verb_exposed(view_cls, verb, write_methods):
    """Honour `mcp_expose` (False = hide entirely; list = explicit verbs)
    and the resource's own `allowed_methods`."""
    expose = getattr(view_cls, 'mcp_expose', None)
    if expose is False:
        return False
    if isinstance(expose, (list, tuple, set)) and verb not in expose:
        return False
    template = _VERB_TEMPLATES[verb]
    if template['verb_method'] not in (view_cls.allowed_methods or []):
        return False
    return True


def _custom_route_tool(view_cls, route, label, components):
    func_name = route['func']
    inst = view_cls()
    handler = getattr(inst, func_name, None)
    meta = getattr(handler, '__openapi__', {}) if handler else {}

    summary = meta.get('summary') or func_name
    description = meta.get('description') or f'{summary} on {label}.'

    input_schema = {'type': 'object', 'properties': {}, 'additionalProperties': True}
    if meta.get('request') and is_pydantic_model(meta['request']):
        input_schema = meta['request'].model_json_schema(
            ref_template='#/components/schemas/{model}'
        )

    output_schema = None
    if meta.get('response') and is_pydantic_model(meta['response']):
        output_schema = meta['response'].model_json_schema(
            ref_template='#/components/schemas/{model}'
        )

    methods = route.get('allowed_methods') or ['get']
    is_write = any(m.lower() in ('post', 'patch', 'delete') for m in methods)

    tool_name = f'{_slugify(func_name)}_{label}'
    tool = {
        'name': tool_name,
        'description': description,
        'inputSchema': input_schema,
        'mcp_internal': {
            'kind': 'custom',
            'view_cls': view_cls,
            'route': route,
            'is_write': is_write,
        },
    }
    if output_schema:
        tool['outputSchema'] = output_schema
    return tool


def list_tools(endpoints):
    """Return a list of tool definitions for the given resources."""
    tools = []
    components = {'schemas': {}}
    write_methods = {'post', 'patch', 'delete'}

    for route_pattern, view_cls in endpoints.items():
        if getattr(view_cls, 'mcp_expose', None) is False:
            continue

        label = _resource_label(view_cls)
        custom_description = getattr(view_cls, 'description', None)

        # Standard CRUD verbs
        for verb in ('list', 'get', 'create', 'update', 'delete'):
            if not _verb_exposed(view_cls, verb, write_methods):
                continue
            template = _VERB_TEMPLATES[verb]
            verb_desc = _VERB_DESCRIPTION[verb].format(label=label)
            description = (
                f'{custom_description} {verb_desc}'
                if custom_description else verb_desc
            )

            if verb == 'list':
                input_schema = _list_input_schema(view_cls)
                output_schema = {
                    'type': 'object',
                    'properties': {
                        'meta': {'type': 'object'},
                        'objects': {
                            'type': 'array',
                            'items': _build_response_schema(view_cls, components, kind='list'),
                        },
                    },
                }
            elif verb == 'get':
                input_schema = _detail_input_schema()
                output_schema = _build_response_schema(view_cls, components, kind='detail')
            elif verb == 'create':
                input_schema = _write_input_schema(view_cls, 'create', components)
                output_schema = _build_response_schema(view_cls, components, kind='detail')
            elif verb == 'update':
                input_schema = _write_input_schema(view_cls, 'update', components)
                output_schema = _build_response_schema(view_cls, components, kind='detail')
            else:  # delete
                input_schema = _delete_input_schema()
                output_schema = {'type': 'object'}

            tool_name = f'{verb}_{label}'
            # ``mcp_exclude_fields`` (legacy) removes the field from
            # both schemas. ``sensitive_fields`` keeps it visible — the
            # agent learns the field exists, but the runtime masks the
            # value. The output schema is kept intact for sensitive
            # fields so what the agent sees in tool definitions matches
            # what comes back in payloads.
            mcp_excludes = list(getattr(view_cls, 'mcp_exclude_fields', None) or [])
            _scrub_schema(input_schema, mcp_excludes)
            _scrub_schema(output_schema, mcp_excludes)
            tools.append({
                'name': tool_name,
                'description': description,
                'inputSchema': input_schema,
                'outputSchema': output_schema,
                'mcp_internal': {
                    'kind': verb,
                    'view_cls': view_cls,
                    'route_pattern': route_pattern,
                    'is_write': template['is_write'],
                },
            })

        # Custom routes with @openapi metadata
        for route in (view_cls.routes or []):
            tools.append(_custom_route_tool(view_cls, route, label, components))

    return tools


def list_tools_public(endpoints):
    """Same as `list_tools` but without the `mcp_internal` key — for
    inspection/registry endpoints."""
    return [
        {k: v for k, v in tool.items() if k != 'mcp_internal'}
        for tool in list_tools(endpoints)
    ]
