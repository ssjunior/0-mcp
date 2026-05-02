"""Render a :class:`~zeromcp.gen.config.Config` into a working
Django + 0-mcp project tree.

Layout produced::

    <output>/
        manage.py
        asgi.py
        requirements.txt
        .env.example
        settings/
            __init__.py
            env.py
            settings.py
        router/
            __init__.py
            endpoints.py
            urls.py
        modules/
            __init__.py
            <prefix>/
                __init__.py
                models.py     # all tables sharing this prefix
                resources.py  # one BaseResource per model

Tables are grouped by the first underscore segment of their SQL name —
``contract``, ``contract_item``, ``contract_history`` all land in
``modules/contract/``. Tables without an underscore form a one-table
module of their own. This mirrors the layout the framework's existing
consumer projects already use, so the generated project drops in next
to a hand-written module without surprises.

The generator emits the **minimum viable scaffolding** — see
``project_introspect_generator.md`` in agent memory for the rationale.
The single auto-applied opinion is ``mcp_exclude_fields`` — the list
of columns matching sensitive name patterns is pre-populated to
prevent agents from seeing credentials by accident.
"""
from __future__ import annotations

from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


def _require_jinja():
    try:
        import jinja2  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Project generation requires Jinja2 — install via "
            "`pip install '0-mcp[gen]'`."
        ) from exc
    return jinja2


def _build_env():
    jinja2 = _require_jinja()
    template_dir = files('zeromcp.gen').joinpath('templates')
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


_PYTHON_KEYWORDS = frozenset({
    'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
    'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
    'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
    'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
    'while', 'with', 'yield',
    # Soft keywords (Python 3.10+) and PEP 8 dunders to dodge.
    'match', 'case', 'type', 'self', 'cls',
})


def _safe_field_name(name: str) -> str:
    """Avoid Python keywords as model field identifiers.

    PEP 8 suggests a trailing underscore, but Django rejects those
    with ``fields.E001`` ("Field names must not end with an
    underscore"). We use a ``field_`` prefix instead — predictable,
    accepted by Django, and the actual SQL column is preserved via
    ``db_column`` so the schema mapping stays exact.
    """
    if name in _PYTHON_KEYWORDS:
        return f'field_{name}'
    if not name.isidentifier():
        # Fall back to a sanitised form for genuinely weird column
        # names ("user-id", "1_count", …). Almost never hit in
        # practice but keeps generated code parseable.
        import re
        cleaned = re.sub(r'\W+', '_', name).strip('_') or 'field'
        if cleaned[0].isdigit():
            cleaned = f'field_{cleaned}'
        return cleaned
    if name.endswith('_'):
        # Django also rejects trailing underscores from the original
        # column name (rare but real — some SQL conventions use it
        # as a separator). Sanitise here so the user does not have to.
        return name.rstrip('_') + '_field' if name.rstrip('_') else 'field'
    return name


def _annotate_columns(schema_tables: dict) -> None:
    """Add a ``safe_name`` key to every column dict so templates can
    pick a Python-legal identifier without recomputing per use.

    Idempotent — calling twice is harmless."""
    for tbl in schema_tables.values():
        for col in tbl.get('columns') or []:
            col.setdefault('safe_name', _safe_field_name(col.get('name', '')))


def _editable_zeromcp_path() -> str | None:
    """Return the absolute path to the editable-install root of the
    currently-running ``zeromcp`` package, or ``None`` when it was
    installed normally from PyPI.

    The check walks up from ``zeromcp/__init__.py`` to find a sibling
    ``pyproject.toml`` — the same heuristic ``pip install -e`` uses.
    """
    import zeromcp
    pkg_dir = Path(zeromcp.__file__).resolve().parent
    for candidate in (pkg_dir.parent, pkg_dir.parent.parent):
        if (candidate / 'pyproject.toml').exists() and (candidate / 'zeromcp').is_dir():
            return str(candidate)
    return None


def _module_prefix(db_table: str) -> str:
    """Folder name a table belongs to: first underscore segment.

    ``contract_item`` → ``contract``; ``client`` → ``client``;
    ``django_migrations`` → ``django``. Always lowercase, always a
    valid Python identifier (we replace non-alnum with ``_``).
    """
    import re
    head = (db_table or 'misc').split('_', 1)[0].lower()
    head = re.sub(r'[^a-z0-9_]+', '_', head) or 'misc'
    return head


def _group_by_module(config: 'Config', schema_tables: dict) -> dict[str, list]:
    """Return ``{module_name: [(class_name, table_config, schema_dict), ...]}``."""
    groups: dict[str, list] = defaultdict(list)
    for class_name, tcfg in config.tables.items():
        if not tcfg.expose:
            continue
        module = _module_prefix(tcfg.db_table or class_name)
        groups[module].append((class_name, tcfg, schema_tables.get(class_name)))
    return dict(groups)


def _detect_api_key_model(config: 'Config', schema_tables: dict) -> str | None:
    """Find a model that looks suitable for ``TENANT_USER_API_MODEL``.

    Heuristic: a table with both an ``api_key``-like column and an
    ``email``/``name`` column. Returns ``'<app_label>.<ClassName>'``
    when a single candidate is obvious, otherwise ``None`` (the
    settings template comments out the line so the user picks).
    """
    candidates: list[str] = []
    for class_name, tcfg in config.tables.items():
        if not tcfg.expose:
            continue
        t = schema_tables.get(class_name)
        if not t:
            continue
        cols = {c['name'].lower() for c in t.get('columns') or []}
        if 'api_key' in cols and ('email' in cols or 'name' in cols):
            module = _module_prefix(tcfg.db_table or class_name)
            candidates.append(f'{module}.{class_name}')
    if len(candidates) == 1:
        return candidates[0]
    return None


def _build_fk_lookup(config: 'Config') -> dict[str, str]:
    """Map ``db_table`` → ``'<app_label>.<ClassName>'`` for every
    exposed table. Models reference FKs via this dotted form so Django
    resolves cross-app relations correctly even when our generated
    project splits tables across many ``modules/<prefix>/`` folders.

    Hidden tables are excluded — generated FKs that point at hidden
    targets get rendered as plain class names so Django's check
    framework will fail loudly (the user has explicitly asked to hide
    that target). That is preferable to silently producing dangling
    references."""
    lookup: dict[str, str] = {}
    for class_name, tcfg in config.tables.items():
        if not tcfg.expose:
            continue
        module = _module_prefix(tcfg.db_table or class_name)
        lookup[tcfg.db_table or class_name] = f'{module}.{class_name}'
    return lookup


def generate(
    config: 'Config',
    output_dir: Path,
    schema: dict | None = None,
    creds: dict | None = None,
) -> list[Path]:
    """Render every template into ``output_dir``. Returns the list of
    files written.

    ``schema`` is the raw introspection dict (from
    :func:`~zeromcp.gen.inspector.inspect`). The config carries user
    opinions (expose, sensitive); the schema carries column types
    needed to generate models.

    ``creds`` is an optional dict with ``db_name``, ``db_host``,
    ``db_port``, ``db_user``, ``db_password``. When provided, the
    generator writes a ready-to-run ``.env`` populated with those
    values; ``.env.example`` is always emitted with placeholders for
    safe committing.
    """
    env = _build_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    schema_tables = {t['name']: t for t in (schema or {}).get('tables', [])}
    _annotate_columns(schema_tables)
    grouped = _group_by_module(config, schema_tables)
    fk_lookup = _build_fk_lookup(config)
    api_key_model = _detect_api_key_model(config, schema_tables)
    # When the running ``zeromcp`` is installed in editable mode
    # (typical during framework development) propagate the local path
    # into ``requirements.txt`` so the generated project's ``./run.sh``
    # picks up the same source — otherwise ``uv pip install`` would
    # try to fetch ``0-mcp`` from PyPI before the package is published.
    zeromcp_editable_path = _editable_zeromcp_path()

    # Top-level scaffolding.
    written += _render(env, 'manage.py.j2', output_dir / 'manage.py', config=config)
    written += _render(env, 'asgi.py.j2', output_dir / 'asgi.py', config=config)
    written += _render(
        env, 'requirements.txt.j2', output_dir / 'requirements.txt',
        config=config, zeromcp_editable_path=zeromcp_editable_path,
    )
    # ``.env.example`` always uses placeholders so it is safe to
    # commit. ``.env`` is rendered with the credentials the user
    # actually typed when they ran ``0-mcp init``.
    written += _render(
        env, '.env.example.j2', output_dir / '.env.example',
        config=config, creds=None,
    )
    if creds:
        written += _render(
            env, '.env.example.j2', output_dir / '.env',
            config=config, creds=creds,
        )
    written += _render(env, 'README.md.j2', output_dir / 'README.md', config=config)
    written += _render(env, '.gitignore.j2', output_dir / '.gitignore', config=config)
    run_sh = output_dir / 'run.sh'
    written += _render(env, 'run.sh.j2', run_sh, config=config)
    run_sh.chmod(0o755)

    # settings/
    settings_dir = output_dir / 'settings'
    settings_dir.mkdir(exist_ok=True)
    (settings_dir / '__init__.py').write_text('from .settings import *  # noqa\n')
    written.append(settings_dir / '__init__.py')
    written += _render(env, 'settings/env.py.j2', settings_dir / 'env.py', config=config)
    written += _render(
        env, 'settings/settings.py.j2', settings_dir / 'settings.py',
        config=config, modules=sorted(grouped.keys()),
        api_key_model=api_key_model,
    )

    # router/
    router_dir = output_dir / 'router'
    router_dir.mkdir(exist_ok=True)
    (router_dir / '__init__.py').write_text('')
    written.append(router_dir / '__init__.py')
    written += _render(
        env, 'router/endpoints.py.j2', router_dir / 'endpoints.py',
        config=config, grouped=grouped,
    )
    written += _render(
        env, 'router/urls.py.j2', router_dir / 'urls.py',
        config=config,
    )

    # modules/<prefix>/{models.py, resources.py}
    modules_dir = output_dir / 'modules'
    modules_dir.mkdir(exist_ok=True)
    (modules_dir / '__init__.py').write_text('')
    written.append(modules_dir / '__init__.py')

    for module_name, members in grouped.items():
        mod_dir = modules_dir / module_name
        mod_dir.mkdir(exist_ok=True)
        (mod_dir / '__init__.py').write_text('')
        written.append(mod_dir / '__init__.py')
        # No ``apps.py`` per module — Django auto-creates the
        # ``AppConfig`` from the dotted path in ``INSTALLED_APPS``,
        # picking up ``DEFAULT_AUTO_FIELD`` from settings.
        written += _render(
            env, 'module/models.py.j2', mod_dir / 'models.py',
            config=config, module_name=module_name, members=members,
            fk_lookup=fk_lookup,
        )
        written += _render(
            env, 'module/resources.py.j2', mod_dir / 'resources.py',
            config=config, module_name=module_name, members=members,
        )

    return written


def _render(env, template_name: str, target: Path, **ctx) -> list[Path]:
    template = env.get_template(template_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.render(**ctx))
    return [target]


