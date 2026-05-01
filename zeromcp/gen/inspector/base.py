"""Database-agnostic schema model + abstract inspector interface.

The output of ``Inspector.introspect()`` is a ``Schema`` dataclass tree
that captures everything the generator needs — column types, FKs,
indexes, unique constraints, choices derived from CHECK constraints,
column comments. Backend-specific quirks (MySQL ``tinyint(1)`` ↔ bool,
Postgres array types, Postgres enums) get normalised here so downstream
code (config builder, templates) does not branch on the backend.

JSON serialisation: every dataclass is JSON-friendly via ``asdict``.
The generator's CLI writes ``introspection.json`` straight from this
form so the user can review/diff it before generating files.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Column:
    name: str
    db_type: str                 # raw SQL type as the backend reports it
    py_type: str                 # mapped Django field type, e.g. 'CharField'
    null: bool = False
    primary_key: bool = False
    unique: bool = False
    auto_increment: bool = False
    max_length: int | None = None
    decimal_digits: tuple[int, int] | None = None  # (max_digits, decimal_places)
    default: Any = None
    is_default_callable: bool = False
    fk_target: str | None = None      # 'schema.table.column' dotted path
    choices: list[tuple[Any, str]] | None = None  # derived from CHECK / enums
    comment: str | None = None        # SQL COMMENT — becomes verbose_name
    # Convenience: original column name as it appears in the database
    # (kept separate from ``name`` because generators may sanitise the
    # latter for Python identifier rules).
    db_column: str | None = None


@dataclass
class Index:
    name: str
    columns: list[str]
    unique: bool = False


@dataclass
class Table:
    name: str                    # cleaned Python class basis (e.g. 'Client')
    db_table: str                # actual SQL table name
    schema: str | None = None
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    unique_together: list[list[str]] = field(default_factory=list)
    comment: str | None = None
    row_estimate: int | None = None  # PG: pg_class.reltuples; MySQL: information_schema.tables.TABLE_ROWS
    # ``True`` when the table has more than one PK column — Django < 5.2
    # cannot model these, so the config builder defaults the table to
    # ``expose: false`` and the CLI warns the user.
    composite_pk: bool = False

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.primary_key]


@dataclass
class Relationship:
    """Explicit relationship link — convenient for the generator and
    for the config builder. Mirrors ``Column.fk_target`` but exposes the
    pair so callers can build a graph without re-parsing strings."""
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on_delete: str = 'DO_NOTHING'  # raw SQL action; mapped to Django at template time


@dataclass
class Schema:
    """Top-level introspection result for one database."""
    backend: str                 # 'postgres' | 'mysql'
    database: str                # database / schema name
    tables: list[Table] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def get_table(self, name: str) -> Table | None:
        for t in self.tables:
            if t.name == name or t.db_table == name:
                return t
        return None


class Inspector:
    """Abstract base for backend-specific introspection.

    Subclasses implement ``connect()`` and ``introspect()``. Connection
    handling is opaque — subclasses pick their own driver.
    """

    backend: str = ''

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self):
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def introspect(self) -> Schema:
        raise NotImplementedError

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
