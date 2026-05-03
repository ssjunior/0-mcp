import json
import re

from django.urls import path, re_path
from django.http import HttpResponse, JsonResponse

from .calc_resource import Metrics
from .openapi import SCALAR_HTML, build_spec
from .redis_config import KEY_PREFIX, get_redis, getex as redis_getex
from .settings_helper import get_cookie_id, get_session_ttl
from .util import validate_session_key
from .tenant.tenant import get_api_session

COOKIE_ID = get_cookie_id()
SESSION_TTL = get_session_ttl()


def get_route(route, view):
    if hasattr(view, 'as_view'):
        return re_path(rf'{route}', view.as_view())
    return path(route, view)


async def _has_valid_session(request):
    """Lightweight auth check for /docs and /openapi.json. Accepts a valid
    session cookie or X-Api-Key. Avoids importing BaseResource here."""
    api_key = request.headers.get('X-Api-Key')
    if api_key:
        session = await get_api_session(api_key)
        return bool(session)

    session_key = validate_session_key(request.COOKIES.get(COOKIE_ID))
    if not session_key:
        return False
    redis = get_redis()
    raw = await redis_getex(
        redis, f'{KEY_PREFIX}sessions:{session_key}', ex=SESSION_TTL,
    )
    return bool(raw)


def get_routes(endpoints, **kwargs):
    """Build the URL patterns for the given resources.

    Always registers ``/openapi.json`` and ``/docs``. Pass ``docs_public=True``
    to make them anonymous; default is ``False`` (requires the same auth as
    the rest of the API — session cookie or X-Api-Key).
    """
    spec_title = kwargs.get('title', '0-mcp')
    spec_version = kwargs.get('version', '1.0.0')
    spec_description = kwargs.get('description')
    docs_public = kwargs.get('docs_public', False)

    async def openapi_json(request, *args, **kwargs):
        if not docs_public and not await _has_valid_session(request):
            return JsonResponse(
                {'success': False, 'status': 401, 'detail': 'Not authorized'},
                status=401,
            )
        spec = build_spec(
            endpoints, title=spec_title, version=spec_version,
            description=spec_description,
        )
        return JsonResponse(spec, json_dumps_params={'indent': 2})

    async def docs_scalar(request, *args, **kwargs):
        if not docs_public and not await _has_valid_session(request):
            return JsonResponse(
                {'success': False, 'status': 401, 'detail': 'Not authorized'},
                status=401,
            )
        return HttpResponse(SCALAR_HTML)

    has_mcp = bool(kwargs.get('mcp'))

    async def index(request, *args, **kwargs):
        if not docs_public and not await _has_valid_session(request):
            return JsonResponse(
                {'success': False, 'status': 401, 'detail': 'Not authorized'},
                status=401,
            )
        return HttpResponse(_render_index_html(
            title=spec_title,
            version=spec_version,
            description=spec_description,
            endpoints=endpoints,
            has_mcp=has_mcp,
        ))

    routes = [
        get_route(key, value) for key, value in endpoints.items()
    ] + [
        path('metrics', Metrics.as_view()),
        path('openapi.json', openapi_json),
        path('docs', docs_scalar),
        path('', index),
    ]

    if kwargs.get('mcp'):
        from .mcp.resource import mcp_view
        from .mcp.tools import list_tools_public

        # Forward attrs to ``mcp_view`` so callers can override
        # ``MCPResource`` defaults (e.g. ``authenticated=False`` for
        # demo deploys) without bypassing ``get_routes`` and re-wiring
        # the route by hand.
        mcp_kwargs = kwargs.get('mcp_kwargs') or {}

        async def mcp_tools_json(request, *args, **kwargs):
            if not docs_public and not await _has_valid_session(request):
                return JsonResponse(
                    {'success': False, 'status': 401, 'detail': 'Not authorized'},
                    status=401,
                )
            return JsonResponse(
                {'tools': list_tools_public(endpoints)},
                json_dumps_params={'indent': 2},
            )

        async def mcp_tools_html(request, *args, **kwargs):
            if not docs_public and not await _has_valid_session(request):
                return JsonResponse(
                    {'success': False, 'status': 401, 'detail': 'Not authorized'},
                    status=401,
                )
            tools = list_tools_public(endpoints)
            return HttpResponse(_render_mcp_tools_html(tools))

        routes += [
            path('mcp', mcp_view(endpoints, **mcp_kwargs)),
            path('mcp/tools.json', mcp_tools_json),
            path('mcp/tools', mcp_tools_html),
        ]

    return routes


_MCP_TOOLS_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>MCP tools — 0-mcp</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font: 13px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; color: #1c2128; background: #ffffff; }}
  .layout {{ display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }}
  aside.sidebar {{ background: #f6f8fa; border-right: 1px solid #d0d7de; padding: 1rem 0.75rem; overflow-y: auto; max-height: 100vh; position: sticky; top: 0; }}
  aside h1 {{ font-size: 0.6875rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #57606a; margin: 0 0 0.25rem; padding: 0 0.5rem; }}
  aside .meta {{ font-size: 0.6875rem; color: #6e7781; margin-bottom: 0.75rem; padding: 0 0.5rem; }}
  aside .meta a {{ color: #0969da; text-decoration: none; }}
  aside .meta a:hover {{ text-decoration: underline; }}
  details.resource {{ margin: 0; }}
  details.resource > summary {{ cursor: pointer; padding: 0.25rem 0.5rem; border-radius: 6px; font-weight: 500; font-size: 0.8125rem; color: #1c2128; list-style: none; display: flex; align-items: center; gap: 0.375rem; line-height: 1.4; user-select: none; }}
  details.resource > summary::-webkit-details-marker {{ display: none; }}
  details.resource > summary:hover {{ background: #eaeef2; }}
  details.resource > summary::before {{ content: ""; width: 0; height: 0; border-left: 4px solid #57606a; border-top: 3px solid transparent; border-bottom: 3px solid transparent; transition: transform 0.15s ease; flex-shrink: 0; }}
  details.resource[open] > summary::before {{ transform: rotate(90deg); }}
  details.resource summary .meta {{ color: #6e7781; font-weight: 400; font-size: 0.6875rem; padding: 0; margin: 0 0 0 auto; }}
  details.resource ul {{ list-style: none; margin: 0; padding: 0 0 0 1rem; }}
  details.resource ul a {{ display: flex; align-items: center; padding: 0.25rem 0.5rem; font-family: inherit; font-size: 0.8125rem; color: #424a53; text-decoration: none; border-radius: 6px; line-height: 1.4; gap: 0.4rem; }}
  details.resource ul a:hover {{ color: #1c2128; background: #eaeef2; }}
  details.resource ul a.active {{ color: #1c2128; background: #ddf4ff; font-weight: 500; }}
  .verb-badge {{ display: inline-block; min-width: 3.25rem; padding: 0.05rem 0.3rem; border-radius: 3px; font-size: 0.625rem; font-weight: 600; text-align: center; text-transform: uppercase; letter-spacing: 0.02em; flex-shrink: 0; }}
  .verb-list {{ background: #dbeafe; color: #1e40af; }}
  .verb-get {{ background: #d1fae5; color: #065f46; }}
  .verb-create {{ background: #fef3c7; color: #92400e; }}
  .verb-update {{ background: #fde68a; color: #78350f; }}
  .verb-delete {{ background: #fee2e2; color: #991b1b; }}
  .verb-other {{ background: #e5e7eb; color: #4b5563; }}
  main {{ padding: 2rem 2.5rem; max-width: 1400px; }}
  main h1 {{ font-size: 1.5rem; margin: 0 0 0.25rem; }}
  main .lead {{ color: #6b7280; margin: 0 0 2rem; }}
  article.tool {{ scroll-margin-top: 1rem; padding: 1.5rem 0 2rem; border-bottom: 1px solid #e5e7eb; }}
  article.tool:last-child {{ border-bottom: none; }}
  article.tool h2 {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 1.25rem; margin: 0 0 0.5rem; color: #0f766e; }}
  article.tool .desc {{ color: #374151; margin: 0 0 1.25rem; }}
  article.tool h3 {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #6b7280; margin: 0 0 0.5rem; }}
  .schemas {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }}
  .schemas > section {{ min-width: 0; }}
  pre {{ background: #1f2937; color: #f3f4f6; padding: 0.875rem 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.8125rem; line-height: 1.45; margin: 0; }}
  @media (max-width: 1080px) {{ .schemas {{ grid-template-columns: 1fr; }} }}
  .empty {{ color: #6b7280; font-style: italic; }}
  @media (max-width: 720px) {{ .layout {{ grid-template-columns: 1fr; }} aside.sidebar {{ position: static; max-height: none; }} }}
</style>
</head><body>
<div class="layout">
<aside class="sidebar">
  <h1>MCP Tools</h1>
  <p class="meta">{count} tool(s) · <a href="tools.json">JSON</a> · <a href="../docs">REST</a></p>
  {sidebar}
</aside>
<main>
  <h1>MCP tools</h1>
  <p class="lead">Generated from your resources — pick a tool on the left to inspect its schemas.</p>
  {body}
</main>
</div>
<script>
  // Highlight the active tool in the sidebar as the user scrolls.
  const links = document.querySelectorAll('aside.sidebar ul a');
  const observer = new IntersectionObserver((entries) => {{
    entries.forEach(e => {{
      if (e.isIntersecting) {{
        links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + e.target.id));
      }}
    }});
  }}, {{ rootMargin: '-30% 0px -60% 0px' }});
  document.querySelectorAll('article.tool').forEach(a => observer.observe(a));
  // Auto-open the resource section that contains the linked tool.
  if (location.hash) {{
    const target = document.querySelector(`aside.sidebar a[href="${{location.hash}}"]`);
    if (target) {{
      const detail = target.closest('details.resource');
      if (detail) detail.open = true;
      target.scrollIntoView({{ block: 'center' }});
    }}
  }}
</script>
</body></html>"""


_VERB_ORDER = {'list': 0, 'get': 1, 'create': 2, 'update': 3, 'delete': 4}


def _split_tool_name(name: str):
    """``list_client`` → ``('list', 'client')``. Custom routes use the
    function name as the verb, and the resource label as the suffix
    after the last underscore."""
    if '_' not in name:
        return ('', name)
    verb, _, label = name.partition('_')
    return (verb, label)


def _render_mcp_tools_html(tools):
    if not tools:
        sidebar = '<p class="empty">No tools.</p>'
        body = '<p class="empty">No MCP tools registered yet.</p>'
        return _MCP_TOOLS_TEMPLATE.format(count=0, sidebar=sidebar, body=body)

    from collections import defaultdict
    grouped = defaultdict(list)
    for t in tools:
        verb, label = _split_tool_name(t.get('name', ''))
        grouped[label].append((verb, t))

    sidebar_parts = []
    body_parts = []
    for label in sorted(grouped):
        items = sorted(grouped[label], key=lambda v: (_VERB_ORDER.get(v[0], 99), v[0]))
        sidebar_parts.append(_render_sidebar_section(label, items))
        for verb, tool in items:
            body_parts.append(_render_tool(tool, verb))
    return _MCP_TOOLS_TEMPLATE.format(
        count=len(tools),
        sidebar='\n'.join(sidebar_parts),
        body='\n'.join(body_parts),
    )


def _render_sidebar_section(label, items):
    lis = []
    for verb, tool in items:
        verb_class = f'verb-{verb}' if verb in ('list', 'get', 'create', 'update', 'delete') else 'verb-other'
        verb_label = verb or '·'
        anchor = f'tool-{tool["name"]}'
        lis.append(
            f'<li><a href="#{anchor}"><span class="verb-badge {verb_class}">{verb_label}</span>{tool["name"]}</a></li>'
        )
    return (
        f'<details class="resource"><summary>{label} <span class="meta">({len(items)})</span></summary>'
        f'<ul>{"".join(lis)}</ul></details>'
    )


_INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{title} — 0-mcp</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 760px; margin: 3rem auto; padding: 0 1rem; color: #1f2937; }}
  h1 {{ font-size: 1.75rem; margin-bottom: 0.25rem; }}
  .lead {{ color: #6b7280; margin-bottom: 2rem; }}
  .version {{ color: #9ca3af; font-size: 0.875rem; }}
  h2 {{ font-size: 1rem; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280;
       margin: 2rem 0 0.5rem; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ border-bottom: 1px solid #e5e7eb; padding: 0.75rem 0; }}
  li:last-child {{ border-bottom: none; }}
  a {{ color: #0f766e; text-decoration: none; font-family: ui-monospace, monospace; font-weight: 500; }}
  a:hover {{ text-decoration: underline; }}
  .desc {{ display: block; color: #6b7280; font-size: 0.875rem; font-family: system-ui, sans-serif;
           font-weight: normal; margin-top: 0.125rem; }}
  .badge {{ display: inline-block; padding: 0.125rem 0.5rem; background: #f0fdfa; color: #0f766e;
            border-radius: 4px; font-size: 0.7rem; margin-left: 0.5rem; vertical-align: middle; }}
  .empty {{ color: #9ca3af; font-style: italic; }}
  footer {{ margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid #e5e7eb;
            color: #9ca3af; font-size: 0.8125rem; }}
</style>
</head><body>
<h1>{title}</h1>
<p class="lead">{description}<span class="version"> · v{version}</span></p>

<h2>Documentation</h2>
<ul>
{mcp_section}
  <li><a href="docs">/docs</a> <span class="badge">Scalar UI</span>
    <span class="desc">Interactive REST API reference — try requests in the browser.</span></li>
  <li><a href="openapi.json">/openapi.json</a> <span class="badge">spec</span>
    <span class="desc">OpenAPI 3.0.3 spec — generated from your resources, ready for codegen.</span></li>
</ul>

<footer>
  Generated by <a href="https://github.com/ssjunior/0-mcp">0-mcp</a>.
</footer>
</body></html>"""


def _render_index_html(*, title, version, description, endpoints, has_mcp):
    description = description or 'REST API generated by 0-mcp.'

    if has_mcp:
        mcp_section = """
  <li><a href="mcp/tools">/mcp/tools</a> <span class="badge">agents</span>
    <span class="desc">MCP tool registry — what LLM agents see and can call.</span></li>
  <li><a href="mcp/tools.json">/mcp/tools.json</a> <span class="badge">spec</span>
    <span class="desc">Tool definitions in JSON format.</span></li>"""
    else:
        mcp_section = ''

    return _INDEX_TEMPLATE.format(
        title=title,
        version=version,
        description=description,
        mcp_section=mcp_section,
    )


def _render_tool(tool, verb=''):
    name = tool.get('name', '')
    desc = tool.get('description', '') or '<span class="empty">no description</span>'
    input_schema = json.dumps(tool.get('inputSchema') or {}, indent=2, ensure_ascii=False)
    output_schema = tool.get('outputSchema')
    verb_class = f'verb-{verb}' if verb in ('list', 'get', 'create', 'update', 'delete') else 'verb-other'
    verb_label = verb or '·'
    parts = [
        f'<article class="tool" id="tool-{name}">',
        f'<h2><span class="verb-badge {verb_class}">{verb_label}</span>{name}</h2>',
        f'<p class="desc">{desc}</p>',
        '<div class="schemas">',
        '<section><h3>Input schema</h3>',
        f'<pre>{input_schema}</pre></section>',
    ]
    if output_schema:
        out_str = json.dumps(output_schema, indent=2, ensure_ascii=False)
        parts += [
            '<section><h3>Output schema</h3>',
            f'<pre>{out_str}</pre></section>',
        ]
    parts += ['</div>', '</article>']
    return ''.join(parts)
