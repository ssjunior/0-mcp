# 0-mcp

A framework I built for myself. It turns Django models into MCP tools and a REST API from one class. If you don't have Django, `0-mcp init` reads a MySQL or Postgres schema and generates the whole project.

I run it in six of my own products. Sharing it because I'd like help making it better — issues, PRs, and "this broke for me" reports are all welcome.

## Install

```bash
pip install django-zeromcp                   # framework only
pip install 'django-zeromcp[gen-mysql]'      # + generator for MySQL
pip install 'django-zeromcp[gen-postgres]'   # + generator for Postgres
```

> The PyPI distribution is `django-zeromcp`. Imports use `from zeromcp
> import ...`. The brand "0-mcp" lives in the docs, the domain, and
> the `0-mcp` CLI.

---

## What it looks like

If you're already on Django:

```python
from zeromcp import BaseResource
from myapp.models import Space

class SpaceResource(BaseResource):
    model = Space
```

That class gives you:

- An MCP server with `list_spaces`, `get_space`, `create_space`, `update_space`, `delete_space` — typed tools, JSON Schema, stdio + HTTP transports.
- A REST API with pagination, filters, search, ordering, an OpenAPI 3.0.3 spec, and an interactive docs page.
- Async dispatch, session + API-key auth, per-IP rate limit, scanner blocking, multi-tenant DB routing.

If you don't have Django yet:

```bash
0-mcp init
```

The CLI prompts for host, db, credentials. About ten seconds later you have a working Django project — every table is a model, every model is an MCP tool. Sensitive columns (`password`, `token`, `api_key`) are auto-masked. Read-only by default. Pass `--writable` when you mean it.

---

## Why it exists

Two years ago I got tired of writing the same Django REST API for the tenth time — DRF, Ninja, FastAPI, all powerful, all the same boilerplate. So I wrote a small framework for myself: one class, set some attributes, get the endpoints. I called it easyapi.

When MCP showed up and every project I had needed an agent surface, I expected to write a second codebase. Instead the MCP server fell out of the same engine in a weekend — auth was already there, rate limit was already there, the field whitelists were already there. Only the wire format changed.

REST is mostly a solved problem now. The new pain is MCP — most teams are rebuilding the same scaffolding. So I renamed the framework and put it on GitHub. **0-mcp** — because that's how much work it should take.

---

## What you get

- **REST + MCP from one class.** Same auth, same fields, same validation.
- **Async end-to-end.** Async ORM, async Redis, async dispatch.
- **Cache with namespace invalidation.** Writes don't blow away unrelated rows.
- **Edge security middleware.** Scanner blocking, 4xx flood detection.
- **Multi-tenant DB routing.** One call switches the connection for the request.
- **Pydantic when you want it.** Otherwise falls back to Django field introspection.
- **OpenAPI 3.0.3 + Scalar UI.** Generated from the same resources.
- **Ownership scoping.** One attribute (`owner_field = 'owner_id'`) restricts every CRUD operation (GET, LIST, PATCH, DELETE) to rows owned by the authenticated user — the cheapest IDOR defense I know. POST also forces `owner_id` to the caller; opt-in `allow_owner_override = True` for admin paths.
- **Sliding session TTLs.** Both cookie and API-key sessions auto-renew on use via Redis `GETEX`. Configure via `SESSION_TTL` (default 1800s) and `API_SESSION_TTL` (default 300s).
- **Global read-only switch.** `MCP = {'READ_ONLY': True}` rejects every non-`GET` request with `405` across all resources — single setting, no per-resource edits. Default `False`. The generator emits this for you when you pick read-only at `0-mcp init`.

Full docs: [link to docs site]

---

## Connecting an agent

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "myapp": {
      "command": "python",
      "args": ["manage.py", "mcp_serve", "myapp.urls.endpoints"],
      "cwd": "/path/to/your/project",
      "env": {
        "MCP_API_KEY": "your-token-here",
        "DJANGO_SETTINGS_MODULE": "myapp.settings"
      }
    }
  }
}
```

Restart Claude Desktop. The agent now has typed tools for every resource you exposed.

For HTTP-based agents (Cursor, custom copilots, anything else that speaks JSON-RPC over POST), the same tools live at `POST /mcp` behind an `X-Api-Key`:

```bash
curl -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H "X-Api-Key: $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## When this isn't the right tool

I'd rather you bounce now than get stuck a month in.

- **No Redis available.** Sessions, cache, rate limit and abuse blocking all rely on it. Redis 6.2+ (uses `GETEX` for sliding session TTLs). Non-negotiable.
- **You need complex auth.** OAuth2 server, SAML, intricate permission matrices — DRF or a custom stack will fit better.
- **Your endpoints are mostly RPC, not CRUD.** And you don't want them as MCP tools either.
- **You don't want Django.** 0-mcp wraps the Django ORM. The `init` command generates a Django project. If that's a dealbreaker, this isn't your tool.
- **You want a big plugin ecosystem.** It's small on purpose.

---

## Hardening before you ship

Defaults are demo-friendly. Production deployments should:

- **Cookie auth.** Set at least one of `ENFORCE_TOKEN = True` (HMAC anti-replay on state-changing requests) or `ALLOWED_ORIGINS = [...]` (Origin allowlist). Without either, the framework logs a startup warning — there is no built-in CSRF defense for `POST/PATCH/DELETE`.
- **Per-resource ownership.** Set `owner_field = 'owner_id'` on resources where rows belong to a single user — restricts every CRUD operation to the row's owner. `POST` always forces `owner_id` to the caller (override with `allow_owner_override = True` for admin paths).
- **Authenticated cache.** If you turn on `cache = True` for an authenticated resource, set `session_cache = True` or `cache_scope_fields = (...)` — otherwise responses can leak across users. The framework warns at runtime when it detects this combination.
- **Tune session TTLs.** `SESSION_TTL` (cookies, default 1800s) and `API_SESSION_TTL` (api-key cache, default 300s) both slide on use. Pick numbers that match your security/UX trade-off.

---

## Help wanted

If you try it and something breaks, please tell me. The kinds of help that make this better:

- **Bug reports.** Open an issue with what you tried and what happened. Including the Python/Django version helps.
- **PRs.** Small ones welcome. For larger changes, open an issue first so we can talk through the shape.
- **"This is confusing"** feedback on the docs. The doc site needs more eyes.
- **Sharing how you use it.** I'm curious what shapes of projects this actually lands in.

There's no CLA, no contributor matrix, no roadmap voting. Just open an issue and we figure it out.

---

## Project

- **Author** — Stamatios Stamou Jr
- **License** — MIT
- **Python** — 3.10+
- **Django** — 5.0+
- **Repo** — github.com/ssjunior/0-mcp

