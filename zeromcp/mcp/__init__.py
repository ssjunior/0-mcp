"""MCP (Model Context Protocol) server for 0-mcp resources.

Speaks JSON-RPC 2.0 over a single endpoint (HTTP POST or stdio). Tool
definitions are auto-generated from the same ``endpoints`` registry used
by :func:`zeromcp.get_routes` — Pydantic schemas, ``summary``/``description``,
field whitelists and custom routes flow through.

Tool calls are translated to synthetic Django requests and run through
``BaseResource.dispatch`` — same auth, rate limit, security middleware,
hooks and validation as the REST API. Zero parallel handlers.

MCP support is bundled in the base install — no extras needed.

Public API:
    list_tools(endpoints)            → list[dict]   tool definitions
    list_tools_public(endpoints)     → list[dict]   without internal meta
    MCPResource                      → BaseResource subclass to subclass
    handle_rpc(message, tools, ctx)  → dict         pure JSON-RPC dispatch
"""
from .tools import list_tools, list_tools_public  # noqa: F401
from .protocol import handle_rpc  # noqa: F401
from .resource import MCPResource  # noqa: F401
