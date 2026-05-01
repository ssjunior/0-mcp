"""Config schema + IO. The config is the user-editable bridge between
introspection and code generation.

Shape::

    project:
      name: billing
      backend: postgres            # 'postgres' | 'mysql'
      database: public

    tables:
      client:                       # logical name (PascalCased class)
        expose: true
        db_table: client            # actual SQL table
        sensitive: [internal_notes] # appended to mcp_exclude_fields default

      django_migrations:
        expose: false               # hide system tables from MCP and REST

YAML is loaded/dumped with ``PyYAML``. We avoid every other YAML option
(no anchors, no custom types) so the file stays diffable and a human
can hand-edit without tripping the parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_SENSITIVE_EXACT = frozenset({
    'password', 'pwd', 'passwd',
    'secret', 'secret_key',
    'api_key', 'api_secret',
    'token', 'access_token', 'refresh_token',
    'private_key', 'session_key',
    'otp', 'otp_secret', 'two_factor_secret',
    'token_password', 'token_user',
})

# Substring patterns are deliberately narrow — too eager and we'd flag
# columns like ``force_password_reset`` (a boolean flag) as sensitive.
_SENSITIVE_PREFIXES = ('password_', 'token_', 'secret_', 'api_key_', 'otp_')
_SENSITIVE_SUFFIXES = ('_password', '_token', '_secret', '_api_key', '_apikey')


_DJANGO_SYSTEM_PREFIXES = (
    'django_', 'auth_',
)


@dataclass
class TableConfig:
    expose: bool = True
    db_table: str = ''
    sensitive: list[str] = field(default_factory=list)


@dataclass
class Project:
    name: str
    backend: str
    database: str
    # When True (default), every generated resource ships with
    # ``allowed_methods = ['get']`` — write verbs (POST/PATCH/DELETE)
    # are kept off both the REST and MCP surface. Safer first-run
    # default for projects pointed at an existing database; flip with
    # ``--writable`` (or edit per-resource) when you want mutations.
    read_only: bool = True
    # Reserved — currently unused. The generator now always emits a
    # demo-friendly project (DEBUG defaults true, public docs, open
    # MCP) and the README documents how to harden for production.
    # Kept on the dataclass so existing config.yaml files still load.
    dev_mode: bool = True
    # When True, generated models drop ``managed = False`` so Django
    # owns the schema — ``makemigrations`` produces an initial set
    # that ``run.sh`` then fakes via ``migrate --fake-initial``. When
    # False (default), Django never touches the tables and the user
    # keeps managing the schema externally (raw SQL, dbt, …).
    django_managed: bool = False


@dataclass
class Config:
    project: Project
    tables: dict[str, TableConfig] = field(default_factory=dict)


def _looks_sensitive(column_name: str) -> bool:
    name = column_name.lower()
    if name in _SENSITIVE_EXACT:
        return True
    if any(name.startswith(p) for p in _SENSITIVE_PREFIXES):
        return True
    if any(name.endswith(s) for s in _SENSITIVE_SUFFIXES):
        return True
    return False


def _is_django_system(db_table: str) -> bool:
    """Tables Django creates for itself (migrations, auth, sessions,
    admin, content types). Default to ``expose: false`` — the user can
    flip them back if they really want to surface them via REST/MCP."""
    name = (db_table or '').lower()
    return any(name.startswith(p) for p in _DJANGO_SYSTEM_PREFIXES)


def _matches_pattern(name: str, patterns: list[str]) -> bool:
    """Glob-style ``*`` match used by ``--include`` / ``--exclude``."""
    import fnmatch
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def build_starter_config(
    introspection: dict,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    read_only: bool = True,
    dev_mode: bool = True,
    django_managed: bool = False,
) -> Config:
    """Take the raw output of ``inspect()`` (as a dict) and produce a
    sensible starter ``Config``.

    Every business table is exposed by default; Django's own
    bookkeeping tables come in with ``expose: false``. Columns matching
    sensitive name patterns get pre-filled into ``sensitive`` so the
    user can review before generating.

    Args:
        include: optional list of glob patterns (matched against
            ``db_table``). When set, only tables whose name matches at
            least one pattern are exposed; everything else is excluded.
        exclude: optional list of glob patterns. Tables matching any
            pattern are excluded regardless of ``include``.
        read_only: pass through to ``Project.read_only``.
        dev_mode: pass through to ``Project.dev_mode``.
    """
    project = Project(
        name=introspection.get('database', 'project'),
        backend=introspection.get('backend', 'postgres'),
        database=introspection.get('database', ''),
        read_only=read_only,
        dev_mode=dev_mode,
        django_managed=django_managed,
    )
    tables: dict[str, TableConfig] = {}
    for tbl in introspection.get('tables') or []:
        db_table = tbl.get('db_table', tbl['name'])
        sensitive = [c['name'] for c in tbl.get('columns') or [] if _looks_sensitive(c['name'])]
        # Resolution order: explicit ``--exclude`` wins over everything,
        # then ``--include`` whitelist, then the Django-system default.
        # Composite-PK tables are *exposed* like any other; the model
        # template emits ``models.CompositePrimaryKey`` (Django 5.2+).
        if exclude and _matches_pattern(db_table, exclude):
            expose = False
        elif include:
            expose = _matches_pattern(db_table, include)
        else:
            expose = not _is_django_system(db_table)
        tables[tbl['name']] = TableConfig(
            expose=expose,
            db_table=db_table,
            sensitive=sensitive,
        )
    return Config(project=project, tables=tables)


# ── YAML IO ─────────────────────────────────────────────────────────


def _require_yaml():
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "config IO requires PyYAML — install via "
            "`pip install '0-mcp[gen]'`."
        ) from exc
    return yaml


def dump_yaml(config: Config) -> str:
    yaml = _require_yaml()
    project_payload = {
        'name': config.project.name,
        'backend': config.project.backend,
        'database': config.project.database,
    }
    if config.project.read_only:
        project_payload['read_only'] = True
    if config.project.dev_mode:
        project_payload['dev_mode'] = True
    if config.project.django_managed:
        project_payload['django_managed'] = True
    payload = {
        'project': project_payload,
        'tables': {
            name: {
                'expose': t.expose,
                'db_table': t.db_table,
                **({'sensitive': t.sensitive} if t.sensitive else {}),
            }
            for name, t in config.tables.items()
        },
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def load_yaml(text: str) -> Config:
    yaml = _require_yaml()
    raw: dict[str, Any] = yaml.safe_load(text) or {}
    proj = raw.get('project') or {}
    project = Project(
        name=proj.get('name', 'project'),
        backend=proj.get('backend', 'postgres'),
        database=proj.get('database', ''),
        read_only=bool(proj.get('read_only', False)),
        dev_mode=bool(proj.get('dev_mode', False)),
        django_managed=bool(proj.get('django_managed', False)),
    )
    tables: dict[str, TableConfig] = {}
    for name, t in (raw.get('tables') or {}).items():
        tables[name] = TableConfig(
            expose=bool(t.get('expose', True)),
            db_table=t.get('db_table', name),
            sensitive=list(t.get('sensitive') or []),
        )
    return Config(project=project, tables=tables)
