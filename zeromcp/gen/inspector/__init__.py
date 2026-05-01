"""Backend-specific introspection. Pick the right one via :func:`inspect`.

The single ``inspect(dsn)`` entry point parses the DSN scheme and
returns the introspected :class:`~zeromcp.gen.inspector.base.Schema`.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .base import Column, Index, Inspector, Relationship, Schema, Table  # noqa: F401


def inspect(dsn: str, schema: str | None = None) -> Schema:
    """Connect to the database described by ``dsn`` and return its
    schema. Caller does not need to pick the inspector class — the
    scheme of the URL drives the dispatch.

    Supported schemes: ``postgres``, ``postgresql``, ``mysql``,
    ``mariadb``.
    """
    parsed = urlparse(dsn)
    scheme = (parsed.scheme or '').lower()
    if scheme in ('postgres', 'postgresql'):
        from .postgres import PostgresInspector
        with PostgresInspector(dsn, schema=schema or 'public') as ins:
            return ins.introspect()
    if scheme in ('mysql', 'mariadb'):
        from .mysql import MySQLInspector
        with MySQLInspector(dsn, schema=schema) as ins:
            return ins.introspect()
    raise ValueError(
        f"Unsupported database scheme: {scheme!r}. "
        f"Use postgres://, postgresql://, mysql:// or mariadb://."
    )
