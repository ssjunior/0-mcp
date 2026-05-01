"""0-mcp public surface.

Heavy imports are wrapped so the CLI generator (``zeromcp.gen.cli``) —
which can be invoked before a Django project even exists — still
loads. Any consumer that uses the framework runtime will have a Django
project and the imports succeed normally; users who only install for
``0-mcp init`` get a working CLI without a stray ``ModuleNotFoundError``
on ``settings``.
"""
try:
    from zeromcp.auth import make_token, validate_token, validate_token_async  # noqa: F401
    from zeromcp.base import BaseResource  # noqa
    from zeromcp.exception import HTTPException  # noqa
    from zeromcp.middleware import AuthMiddleware, ExceptionMiddleware  # noqa
    from zeromcp.security import SecurityMiddleware  # noqa
    from zeromcp.routes import get_routes  # noqa
    from zeromcp.tenant.db_router import DBRouter  # noqa
    from zeromcp.tenant.tenant import (  # noqa: F401
        aset_tenant, db_state, get_account, get_master_user, get_tenant, set_tenant,
    )
    from zeromcp.filters import Filter as OrmFilter, validate_conditions  # noqa
    from zeromcp.redis_config import get_cache_stats, reset_cache_stats  # noqa: F401
    from zeromcp.schemas import openapi  # noqa: F401
    from zeromcp.openapi import build_spec  # noqa: F401
except ModuleNotFoundError as _exc:  # noqa
    # Likely the project's ``settings`` package is not on sys.path —
    # standard when running the ``0-mcp`` CLI before the project
    # exists. We swallow the error here so the gen submodule can load;
    # actual usage of these names (BaseResource etc.) will reraise.
    if _exc.name not in ('settings', 'django'):
        raise

try:  # MCP is an optional extra
    from zeromcp.mcp import MCPResource, list_tools  # noqa: F401
    from zeromcp.mcp.resource import mcp_view  # noqa: F401
except ImportError:
    pass
