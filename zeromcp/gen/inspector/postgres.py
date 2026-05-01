"""Postgres inspector — uses ``psycopg2`` to read ``information_schema``
plus a few PG-specific catalogs (``pg_class`` for row estimates,
``pg_description`` for column/table comments).

Only depends on ``psycopg2`` (or ``psycopg2-binary``) — install via
``pip install '0-mcp[gen-postgres]'`` or ``pip install '0-mcp[gen]'``.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .base import Column, Index, Inspector, Relationship, Schema, Table


_PG_TO_DJANGO = {
    'bigint': 'BigIntegerField',
    'bigserial': 'BigAutoField',
    'boolean': 'BooleanField',
    'bytea': 'BinaryField',
    'character': 'CharField',
    'character varying': 'CharField',
    'date': 'DateField',
    'double precision': 'FloatField',
    'inet': 'GenericIPAddressField',
    'integer': 'IntegerField',
    'json': 'JSONField',
    'jsonb': 'JSONField',
    'numeric': 'DecimalField',
    'real': 'FloatField',
    'serial': 'AutoField',
    'smallint': 'SmallIntegerField',
    'smallserial': 'SmallAutoField',
    'text': 'TextField',
    'time without time zone': 'TimeField',
    'time with time zone': 'TimeField',
    'timestamp without time zone': 'DateTimeField',
    'timestamp with time zone': 'DateTimeField',
    'uuid': 'UUIDField',
}


def _map_django_type(db_type: str) -> str:
    base = db_type.lower()
    base = re.sub(r'\(.*\)', '', base).strip()  # strip "(255)" length etc.
    return _PG_TO_DJANGO.get(base, 'TextField')


def _parse_choices_from_check(constraint_def: str, column: str) -> list | None:
    """Best-effort parse of CHECK (col IN (1,2,3)) → choices.

    Conservative: returns ``None`` for anything that does not match the
    simple ``IN`` form — the user can always declare choices manually
    in the project files later.
    """
    if not constraint_def:
        return None
    pattern = re.compile(
        rf'\b{re.escape(column)}\b\s*=\s*ANY\s*\(\s*ARRAY\[([^\]]+)\]',
        re.IGNORECASE,
    )
    m = pattern.search(constraint_def)
    if not m:
        # Try simpler "column IN (...)"
        pattern2 = re.compile(
            rf'\b{re.escape(column)}\b\s+IN\s*\(([^)]+)\)',
            re.IGNORECASE,
        )
        m = pattern2.search(constraint_def)
    if not m:
        return None
    raw = m.group(1)
    parts = [p.strip().strip("'") for p in raw.split(',')]
    out: list[tuple] = []
    for p in parts:
        try:
            ival = int(p)
            out.append((ival, p))
        except ValueError:
            out.append((p, p))
    return out or None


class PostgresInspector(Inspector):
    backend = 'postgres'

    def __init__(self, dsn: str, schema: str = 'public') -> None:
        super().__init__(dsn)
        self.schema = schema
        self.conn = None

    def connect(self):
        # Use whichever Postgres driver is already on the path.
        # ``psycopg`` (v3) is the modern API; ``psycopg2`` is what
        # most existing Django projects ship. Either works.
        driver = None
        for module_name in ('psycopg2', 'psycopg'):
            try:
                driver = __import__(module_name)
                break
            except ImportError:
                continue
        if driver is None:  # pragma: no cover
            raise RuntimeError(
                "Postgres introspection needs either ``psycopg2`` or "
                "``psycopg`` on the path. Install with "
                "`pip install psycopg2-binary` or "
                "`pip install '0-mcp[gen-postgres]'`."
            )
        # Accept both DSNs and Django-style URLs (``postgres://...``).
        if self.dsn.startswith('postgres://') or self.dsn.startswith('postgresql://'):
            parsed = urlparse(self.dsn)
            self.conn = driver.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                dbname=parsed.path.lstrip('/'),
            )
        else:
            self.conn = driver.connect(self.dsn)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def introspect(self) -> Schema:
        cur = self.conn.cursor()
        # Pre-build a map of Postgres ENUM types → labels so column
        # introspection below can attach choices when it sees one.
        cur.execute(
            """
            SELECT t.typname, e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            ORDER BY t.typname, e.enumsortorder
            """
        )
        self._enum_labels: dict[str, list[str]] = {}
        for typname, label in cur.fetchall():
            self._enum_labels.setdefault(typname, []).append(label)

        # Tables in the target schema
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (self.schema,),
        )
        table_names = [r[0] for r in cur.fetchall()]

        tables: list[Table] = []
        relationships: list[Relationship] = []
        for tname in table_names:
            t = self._introspect_table(cur, tname)
            tables.append(t)
            for col in t.columns:
                if col.fk_target:
                    to_table, to_col = col.fk_target.rsplit('.', 1)
                    relationships.append(Relationship(
                        from_table=t.db_table,
                        from_column=col.db_column or col.name,
                        to_table=to_table.split('.')[-1],
                        to_column=to_col,
                    ))

        # Multi-column primary keys are flagged so the model template
        # emits ``pk = models.CompositePrimaryKey(...)`` (Django 5.2+).
        # Generated projects pin ``Django>=5.2`` precisely so this just
        # works — no need to skip or warn.
        for t in tables:
            if len([c for c in t.columns if c.primary_key]) > 1:
                t.composite_pk = True

        return Schema(
            backend=self.backend,
            database=self.schema,
            tables=tables,
            relationships=relationships,
        )

    def _introspect_table(self, cur, table_name: str) -> Table:
        # Columns + types + nullability + defaults
        cur.execute(
            """
            SELECT column_name, data_type, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable,
                   column_default,
                   col_description(
                     (table_schema || '.' || table_name)::regclass::oid,
                     ordinal_position
                   ) AS comment
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (self.schema, table_name),
        )
        col_rows = cur.fetchall()

        # Primary key columns
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = (%s)::regclass AND i.indisprimary
            """,
            (f'{self.schema}.{table_name}',),
        )
        pk_cols = {r[0] for r in cur.fetchall()}

        # Unique constraints / unique indexes
        cur.execute(
            """
            SELECT i.relname, ix.indisunique, ix.indisprimary,
                   array_agg(a.attname ORDER BY array_position(ix.indkey, a.attnum))
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
            WHERE n.nspname = %s AND t.relname = %s
            GROUP BY i.relname, ix.indisunique, ix.indisprimary
            """,
            (self.schema, table_name),
        )
        indexes: list[Index] = []
        unique_singles: set[str] = set()
        unique_together: list[list[str]] = []
        for iname, is_unique, is_pk, cols in cur.fetchall():
            if is_pk:
                continue
            indexes.append(Index(name=iname, columns=list(cols), unique=is_unique))
            if is_unique and len(cols) == 1:
                unique_singles.add(cols[0])
            elif is_unique:
                unique_together.append(list(cols))

        # Foreign keys
        cur.execute(
            """
            SELECT kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
              AND tc.table_name = %s
            """,
            (self.schema, table_name),
        )
        fk_map = {col: f'{schema}.{tbl}.{ref_col}' for col, schema, tbl, ref_col in cur.fetchall()}

        # CHECK constraints (best-effort choices)
        cur.execute(
            """
            SELECT tc.constraint_name, cc.check_clause
            FROM information_schema.table_constraints tc
            JOIN information_schema.check_constraints cc
              ON tc.constraint_name = cc.constraint_name
             AND tc.constraint_schema = cc.constraint_schema
            WHERE tc.constraint_type = 'CHECK'
              AND tc.table_schema = %s
              AND tc.table_name = %s
            """,
            (self.schema, table_name),
        )
        check_clauses = [r[1] for r in cur.fetchall() if r[1]]

        # Table comment
        cur.execute(
            """
            SELECT obj_description((%s)::regclass::oid)
            """,
            (f'{self.schema}.{table_name}',),
        )
        table_comment = cur.fetchone()[0]

        # Row estimate (cheap, approximate)
        cur.execute(
            """
            SELECT reltuples::bigint
            FROM pg_class WHERE relname = %s AND relnamespace = (
                SELECT oid FROM pg_namespace WHERE nspname = %s
            )
            """,
            (table_name, self.schema),
        )
        row_estimate_row = cur.fetchone()
        row_estimate = int(row_estimate_row[0]) if row_estimate_row else None

        columns: list[Column] = []
        for (col_name, data_type, max_len, num_prec, num_scale, is_null,
             default, comment) in col_rows:
            choices = None
            for cc in check_clauses:
                choices = _parse_choices_from_check(cc, col_name) or choices
            # Postgres ENUM types report as ``USER-DEFINED`` in the SQL
            # standard view; resolve via udt_name in a follow-up query.
            udt_name = None
            if data_type.lower() == 'user-defined':
                cur.execute(
                    """
                    SELECT udt_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                      AND column_name = %s
                    """,
                    (self.schema, table_name, col_name),
                )
                row = cur.fetchone()
                if row:
                    udt_name = row[0]
            if udt_name and udt_name in self._enum_labels:
                labels = self._enum_labels[udt_name]
                choices = [(v, v) for v in labels]
                django_type = 'CharField'
                # ``max_length`` not reported for enum — use the longest
                # label so Django doesn't reject the field.
                max_len = max(len(v) for v in labels)
            else:
                django_type = _map_django_type(data_type)
            decimal_digits = (num_prec, num_scale) if num_prec and num_scale is not None else None
            auto_inc = isinstance(default, str) and 'nextval' in default
            columns.append(Column(
                name=col_name,
                db_type=udt_name or data_type,
                py_type=django_type if not auto_inc else (
                    'BigAutoField' if data_type == 'bigint' else 'AutoField'
                ),
                null=(is_null == 'YES'),
                primary_key=col_name in pk_cols,
                unique=col_name in unique_singles,
                auto_increment=auto_inc,
                max_length=max_len,
                decimal_digits=decimal_digits,
                default=None if auto_inc else default,
                fk_target=fk_map.get(col_name),
                choices=choices,
                comment=comment,
                db_column=col_name,
            ))

        return Table(
            name=_pascal_case(table_name),
            db_table=table_name,
            schema=self.schema,
            columns=columns,
            indexes=indexes,
            unique_together=unique_together,
            comment=table_comment,
            row_estimate=row_estimate,
        )


def _pascal_case(name: str) -> str:
    """``customer_invoice_item`` → ``CustomerInvoiceItem``.

    Strips Django-style duplicated app prefixes — when a segment after
    an underscore starts with the previous segment as a literal prefix,
    we drop the duplicate so ``invoice_invoicehistory`` becomes
    ``InvoiceHistory`` instead of ``InvoiceInvoicehistory``.
    """
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    parts = [p for p in cleaned.split('_') if p]
    deduped: list[str] = []
    for p in parts:
        if not deduped:
            deduped.append(p)
            continue
        prev_lower = deduped[-1].lower()
        p_lower = p.lower()
        # Exact duplicate (``user`` after ``user``) → drop the repeat.
        if p_lower == prev_lower:
            continue
        # ``invoice`` followed by ``invoicehistory`` → keep ``invoice``,
        # then split the trailer (``history``) as its own segment.
        if p_lower.startswith(prev_lower) and len(p) > len(deduped[-1]):
            tail = p[len(deduped[-1]):]
            deduped.append(tail)
            continue
        deduped.append(p)
    return ''.join(part.capitalize() for part in deduped if part)
