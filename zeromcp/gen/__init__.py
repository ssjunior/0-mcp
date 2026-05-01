"""Project generator — read a database, emit a working 0-mcp project.

Public entry points:

- ``0-mcp init --db <url> --output <dir>`` — all-in-one CLI flow.
- ``0-mcp introspect --db <url>`` — emit ``introspection.json`` only.
- ``0-mcp generate <config.yaml> --output <dir>`` — config → project files.

The generator deliberately emits the **minimum viable scaffolding** —
``model = X`` per resource and the framework defaults handle the rest.
Filter/search/cache/FK expansion are user opt-in. The only auto-applied
opinion is ``mcp_exclude_fields`` for columns matching common sensitive
name patterns (``password``, ``token``, ``secret``, etc.) — protects
the user from accidental leaks via MCP tools.
"""

from .inspector.base import Schema, Table, Column, Relationship  # noqa: F401
