"""``MCPResource`` — a ``BaseResource`` subclass that exposes the MCP
JSON-RPC endpoint over HTTP POST.

Inherit from this when you want to customize: override ``post_process``
for audit, ``pre_process`` to inject context, set ``cache_ttl`` to
cache ``tools/list`` responses, etc. All BaseResource hooks apply.

Usage:

    class MyMCP(MCPResource):
        endpoints = my_endpoints       # required: the registry to expose
        summary = 'mcp'

    urlpatterns = [path('mcp/', MyMCP.as_view())]
"""
from django.http import JsonResponse

from ..base import BaseResource
from .protocol import handle_rpc
from .tools import list_tools


class MCPResource(BaseResource):
    """JSON-RPC 2.0 endpoint for the MCP protocol.

    Exposes every resource in ``self.endpoints`` as agent-callable tools.
    """

    authenticated = True
    allowed_methods = ['post']
    cache = False
    summary = 'mcp'
    description = (
        'JSON-RPC 2.0 endpoint exposing every resource as agent-callable tools. '
        'Tool calls are routed through the same dispatch as the REST API.'
    )
    mcp_expose = False  # never expose the MCP endpoint as a tool itself

    #: Set this on subclasses (or via ``mcp_view(endpoints)`` factory).
    endpoints = None

    _tools_cache = None

    def __init__(self):
        super().__init__()
        # Build the registry once per process; tool definitions are
        # static for a given endpoints map.
        cls = type(self)
        if cls._tools_cache is None and cls.endpoints is not None:
            cls._tools_cache = list_tools(cls.endpoints)

    @property
    def tools(self):
        return type(self)._tools_cache or []

    async def post(self, request):
        ctx = {
            'api_key': request.headers.get('X-Api-Key'),
            'cookie': request.headers.get('Cookie'),
            'user': self.user,
            'account': self.account,
            # Propagate the real client envelope so the synthetic request
            # built by the bridge sees the same IP/UA/Host the rate limiter
            # and SecurityMiddleware (incl. ``get_allowed_domain``) would see
            # for a REST hit.
            'remote_addr': request.META.get('REMOTE_ADDR'),
            'forwarded_for': request.META.get('HTTP_X_FORWARDED_FOR'),
            'real_ip': request.META.get('HTTP_X_REAL_IP'),
            'user_agent': request.META.get('HTTP_USER_AGENT'),
            'host': request.headers.get('Host'),
            'referer': request.headers.get('Referer'),
        }
        message = self.body
        result = await handle_rpc(message, self.tools, ctx)
        if result is None:
            # JSON-RPC notifications get no response body
            return JsonResponse({}, status=204)
        return JsonResponse(result, json_dumps_params={'ensure_ascii': False})


def mcp_view(endpoints, **attrs):
    """Build a one-off ``MCPResource`` subclass bound to ``endpoints``.

    Usage::

        path('mcp/', mcp_view(endpoints))
        path('mcp/', mcp_view(endpoints, authenticated=False))
    """
    namespace = {'endpoints': endpoints, **attrs}
    cls = type('MCPResourceConfigured', (MCPResource,), namespace)
    return cls.as_view()
