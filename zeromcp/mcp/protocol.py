"""Pure JSON-RPC 2.0 dispatch for the MCP protocol.

Knows nothing about HTTP or stdio — receives a parsed message dict and
returns a response dict (or None for notifications). Transports
(``MCPResource.post`` and the ``mcp_serve`` management command) wrap
this in their respective IO loops.

Implements:
    initialize          — handshake, returns protocolVersion + serverInfo
    notifications/*     — silently acknowledged
    ping                — no-op
    tools/list          — returns the registry minus internal meta
    tools/call          — validates input, calls the tool via bridge
"""
import logging

from .bridge import call_tool

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = '2025-06-18'

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


SERVER_INFO = {'name': '0-mcp', 'version': '1.0'}


def _rpc_error(rpc_id, code, message):
    return {'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': code, 'message': message}}


def _rpc_result(rpc_id, result):
    return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}


def _tool_response(envelope, is_error=False):
    import json
    return {
        'content': [{'type': 'text', 'text': json.dumps(envelope, default=str, ensure_ascii=False)}],
        'isError': is_error,
    }


def _try_validate(tool, arguments):
    """Best-effort fast-fail validation against the tool's inputSchema.
    Uses jsonschema when installed; otherwise dispatch will revalidate
    via Pydantic / field whitelists. Returns None on success, an error
    envelope on failure.
    """
    try:
        from jsonschema import Draft202012Validator, ValidationError
    except ImportError:
        return None

    try:
        Draft202012Validator(tool.get('inputSchema') or {}).validate(arguments)
    except ValidationError as ve:
        return {
            'tool': tool['name'],
            'code': 'VALIDATION_ERROR',
            'message': ve.message,
            'path': list(ve.absolute_path),
        }
    return None


async def handle_rpc(message, tools, ctx):
    """Dispatch a single JSON-RPC message. ``ctx`` is a dict that travels
    through to ``call_tool`` (api_key, cookie, user). Returns the
    response dict, or ``None`` for notifications."""
    if not isinstance(message, dict):
        return _rpc_error(None, JSONRPC_INVALID_REQUEST, 'Request must be a JSON object')

    rpc_id = message.get('id')
    method = message.get('method')
    params = message.get('params') or {}

    if not method or not isinstance(method, str):
        return _rpc_error(rpc_id, JSONRPC_INVALID_REQUEST, 'Missing method')

    is_notification = rpc_id is None and method.startswith('notifications/')

    try:
        if method == 'initialize':
            result = {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': SERVER_INFO,
            }
        elif method == 'ping':
            result = {}
        elif method.startswith('notifications/'):
            return None
        elif method == 'tools/list':
            result = {
                'tools': [
                    {k: v for k, v in t.items() if k != 'mcp_internal'}
                    for t in tools
                ]
            }
        elif method == 'tools/call':
            tool_name = params.get('name')
            arguments = params.get('arguments') or {}
            tool = next((t for t in tools if t['name'] == tool_name), None)
            if tool is None:
                envelope = {'tool': tool_name or 'unknown', 'code': 'NOT_FOUND',
                            'message': f'Unknown tool: {tool_name}'}
                result = _tool_response(envelope, is_error=True)
            else:
                err = _try_validate(tool, arguments)
                if err is not None:
                    result = _tool_response(err, is_error=True)
                else:
                    try:
                        payload = await call_tool(tool, arguments, ctx)
                        envelope = {'tool': tool_name, 'data': payload}
                        result = _tool_response(envelope, is_error=False)
                    except Exception as exc:
                        code, message = _map_exception(exc)
                        logger.exception('mcp.tool.error tool=%s', tool_name)
                        envelope = {'tool': tool_name, 'code': code, 'message': message}
                        result = _tool_response(envelope, is_error=True)
        else:
            return _rpc_error(rpc_id, JSONRPC_METHOD_NOT_FOUND, f'Unknown method: {method}')
    except Exception as exc:
        logger.exception('mcp.rpc.error method=%s', method)
        return _rpc_error(rpc_id, JSONRPC_INTERNAL_ERROR, str(exc))

    if is_notification:
        return None
    return _rpc_result(rpc_id, result)


def _map_exception(exc):
    """Translate 0-mcp/Django exceptions into MCP-friendly codes."""
    args = getattr(exc, 'args', ())
    status = None
    message = str(exc)
    if args and isinstance(args[0], int):
        status = args[0]
        if len(args) > 1:
            message = str(args[1])
    if status == 401:
        return 'UNAUTHORIZED', message
    if status == 403:
        return 'FORBIDDEN', message
    if status == 404:
        return 'NOT_FOUND', message
    if status in (400, 422):
        return 'VALIDATION_ERROR', message
    if status == 405:
        return 'METHOD_NOT_ALLOWED', message
    if status == 429:
        return 'RATE_LIMITED', message
    return 'INTERNAL', 'Internal server error'
