"""MySQL/MariaDB inspector — uses ``pymysql`` against
``information_schema``. ``tinyint(1)`` is normalised to ``BooleanField``
since that is the convention every Django app on MySQL relies on.

Install: ``pip install '0-mcp[gen-mysql]'`` or ``pip install '0-mcp[gen]'``.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .base import Column, Index, Inspector, Relationship, Schema, Table
from .postgres import _pascal_case, _parse_choices_from_check


_MYSQL_TO_DJANGO = {
    'bigint': 'BigIntegerField',
    'binary': 'BinaryField',
    'blob': 'BinaryField',
    'char': 'CharField',
    'date': 'DateField',
    'datetime': 'DateTimeField',
    'decimal': 'DecimalField',
    'double': 'FloatField',
    'enum': 'CharField',
    'float': 'FloatField',
    'int': 'IntegerField',
    'integer': 'IntegerField',
    'json': 'JSONField',
    'longblob': 'BinaryField',
    'longtext': 'TextField',
    'mediumblob': 'BinaryField',
    'mediumint': 'IntegerField',
    'mediumtext': 'TextField',
    'numeric': 'DecimalField',
    'real': 'FloatField',
    'smallint': 'SmallIntegerField',
    'text': 'TextField',
    'time': 'TimeField',
    'timestamp': 'DateTimeField',
    'tinyblob': 'BinaryField',
    'tinyint': 'IntegerField',
    'tinytext': 'TextField',
    'varbinary': 'BinaryField',
    'varchar': 'CharField',
    'year': 'IntegerField',
}


def _map_django_type(column_type: str, data_type: str) -> str:
    """``tinyint(1)`` → BooleanField, otherwise plain dict lookup."""
    ct = (column_type or '').lower()
    if ct.startswith('tinyint(1)'):
        return 'BooleanField'
    return _MYSQL_TO_DJANGO.get(data_type.lower(), 'TextField')


class MySQLInspector(Inspector):
    backend = 'mysql'

    def __init__(self, dsn: str, schema: str | None = None) -> None:
        super().__init__(dsn)
        self.schema = schema  # populated from URL if not given
        self.conn = None

    def connect(self):
        # Use whichever MySQL driver is already on the path. Most
        # Django-on-MySQL projects ship ``mysqlclient`` (faster, C);
        # ``PyMySQL`` is the pure-Python fallback. Either one is fine
        # for introspection — both are DB-API 2.0 compliant.
        driver = None
        for module_name in ('MySQLdb', 'pymysql'):
            try:
                driver = __import__(module_name)
                break
            except ImportError:
                continue
        if driver is None:  # pragma: no cover
            raise RuntimeError(
                "MySQL introspection needs either ``mysqlclient`` or "
                "``pymysql`` on the path. Install with "
                "`pip install mysqlclient` (recommended for Django) or "
                "`pip install '0-mcp[gen-mysql]'`."
            )

        if self.dsn.startswith('mysql://') or self.dsn.startswith('mariadb://'):
            parsed = urlparse(self.dsn)
            db_name = parsed.path.lstrip('/')
            self.schema = self.schema or db_name
            kwargs = {
                'host': parsed.hostname,
                'port': parsed.port or 3306,
                'user': parsed.username,
                'password': parsed.password,
                'database': db_name,
                'charset': 'utf8mb4',
            }
            self.conn = driver.connect(**kwargs)
        else:
            self.conn = driver.connect(self.dsn)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def introspect(self) -> Schema:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT table_name, table_comment, table_rows
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (self.schema,),
        )
        table_meta = list(cur.fetchall())

        tables: list[Table] = []
        relationships: list[Relationship] = []
        for tname, tcomment, trows in table_meta:
            t = self._introspect_table(cur, tname, tcomment, trows)
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

        for t in tables:
            if len([c for c in t.columns if c.primary_key]) > 1:
                t.composite_pk = True

        return Schema(
            backend=self.backend,
            database=self.schema or '',
            tables=tables,
            relationships=relationships,
        )

    def _introspect_table(self, cur, table_name, table_comment, table_rows) -> Table:
        cur.execute(
            """
            SELECT column_name, data_type, column_type, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable,
                   column_default, extra, column_comment, column_key
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (self.schema, table_name),
        )
        col_rows = cur.fetchall()

        # Indexes (multi-column, unique flags)
        cur.execute(
            """
            SELECT index_name, non_unique,
                   GROUP_CONCAT(column_name ORDER BY seq_in_index)
            FROM information_schema.statistics
            WHERE table_schema = %s AND table_name = %s
            GROUP BY index_name, non_unique
            """,
            (self.schema, table_name),
        )
        indexes: list[Index] = []
        unique_singles: set[str] = set()
        unique_together: list[list[str]] = []
        for iname, non_unique, cols_csv in cur.fetchall():
            cols = [c.strip() for c in (cols_csv or '').split(',') if c.strip()]
            if iname == 'PRIMARY':
                continue
            is_unique = (non_unique == 0)
            indexes.append(Index(name=iname, columns=cols, unique=is_unique))
            if is_unique and len(cols) == 1:
                unique_singles.add(cols[0])
            elif is_unique:
                unique_together.append(cols)

        # FKs
        cur.execute(
            """
            SELECT column_name, referenced_table_schema, referenced_table_name,
                   referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s AND table_name = %s
              AND referenced_table_name IS NOT NULL
            """,
            (self.schema, table_name),
        )
        fk_map = {
            col: f'{ref_schema}.{ref_tbl}.{ref_col}'
            for col, ref_schema, ref_tbl, ref_col in cur.fetchall()
        }

        # CHECK constraints (MySQL 8+ / MariaDB 10.2+)
        check_clauses: list[str] = []
        try:
            cur.execute(
                """
                SELECT cc.check_clause
                FROM information_schema.check_constraints cc
                JOIN information_schema.table_constraints tc
                  ON tc.constraint_name = cc.constraint_name
                 AND tc.constraint_schema = cc.constraint_schema
                WHERE tc.table_schema = %s AND tc.table_name = %s
                """,
                (self.schema, table_name),
            )
            check_clauses = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:
            pass  # CHECK introspection is best-effort

        columns: list[Column] = []
        for (col_name, data_type, column_type, max_len, num_prec, num_scale,
             is_null, default, extra, comment, col_key) in col_rows:
            choices = None
            for cc in check_clauses:
                choices = _parse_choices_from_check(cc, col_name) or choices
            # ENUM('a','b') → choices
            if data_type.lower() == 'enum' and column_type:
                m = re.match(r"enum\((.*)\)", column_type, re.IGNORECASE)
                if m:
                    raw = m.group(1)
                    parts = re.findall(r"'([^']*)'", raw)
                    choices = [(p, p) for p in parts] or choices
            decimal_digits = (num_prec, num_scale) if num_prec and num_scale is not None else None
            auto_inc = bool(extra and 'auto_increment' in extra.lower())
            primary = (col_key == 'PRI')
            py_type = _map_django_type(column_type, data_type)
            if auto_inc and py_type == 'BigIntegerField':
                py_type = 'BigAutoField'
            elif auto_inc and py_type == 'IntegerField':
                py_type = 'AutoField'
            columns.append(Column(
                name=col_name,
                db_type=column_type or data_type,
                py_type=py_type,
                null=(is_null == 'YES'),
                primary_key=primary,
                unique=(col_name in unique_singles or col_key == 'UNI'),
                auto_increment=auto_inc,
                max_length=max_len,
                decimal_digits=decimal_digits,
                default=None if auto_inc else default,
                fk_target=fk_map.get(col_name),
                choices=choices,
                comment=comment or None,
                db_column=col_name,
            ))

        return Table(
            name=_pascal_case(table_name),
            db_table=table_name,
            schema=self.schema,
            columns=columns,
            indexes=indexes,
            unique_together=unique_together,
            comment=table_comment or None,
            row_estimate=int(table_rows) if table_rows is not None else None,
        )
