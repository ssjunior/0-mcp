"""``0-mcp`` CLI — entry point for introspection and project generation.

Subcommands:

- ``introspect`` — connect, dump ``introspection.json`` (raw schema, no
  Django assumptions). Useful for piping to your own tooling.
- ``config`` — turn ``introspection.json`` into a starter
  ``config.yaml`` (one stanza per table, ``expose: true``, sensitive
  columns auto-flagged).
- ``generate`` — render ``config.yaml`` + templates into a working
  Django + 0-mcp project tree.
- ``init`` — convenience wrapper for ``introspect → config → generate``.

Bootstrap is intentionally CLI-only (no Django, no settings module
required) so the user can run it before the project even exists.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path


def _split_csv(value: str | None) -> list[str]:
    """Comma-separated CLI value → list (stripped, dedup)."""
    if not value:
        return []
    return [v.strip() for v in value.split(',') if v.strip()]


def _prompt(question: str, default: str | None = None, *, secret: bool = False) -> str:
    """Tiny TTY prompt with default value support. Returns the user's
    input, or the default when the user just hits Enter."""
    suffix = f' [{default}]' if default else ''
    if secret:
        import getpass
        return getpass.getpass(f'{question}{suffix}: ') or (default or '')
    raw = input(f'{question}{suffix}: ').strip()
    return raw or (default or '')


def _prompt_db_url() -> tuple[str, str | None]:
    """Walk the user through ``host / port / db / user / password``
    one field at a time. Returns ``(dsn, schema)`` — ``schema`` is
    only relevant for Postgres. MySQL is the default engine since
    that is what the framework's existing consumers use."""
    print('0-mcp — interactive mode (Ctrl+C to abort)\n')
    while True:
        backend = _prompt('Database engine (mysql/postgres)', 'mysql').lower()
        if backend in ('mysql', 'mariadb', 'postgres', 'postgresql'):
            break
        print('  ⚠  please answer "mysql" or "postgres"\n')
    host = _prompt('Host', '127.0.0.1')
    default_port = '3306' if backend.startswith('my') or backend.startswith('mariadb') else '5432'
    port = _prompt('Port', default_port)
    database = _prompt('Database name')
    while not database:
        database = _prompt('Database name (required)')
    user = _prompt('User')
    password = _prompt('Password', secret=True)
    schema = None
    if backend.startswith('post'):
        schema = _prompt('Schema', 'public') or None
    scheme = 'mysql' if backend.startswith(('my', 'mariadb')) else 'postgresql'
    from urllib.parse import quote
    user_q = quote(user, safe='')
    pass_q = quote(password, safe='')
    return (f'{scheme}://{user_q}:{pass_q}@{host}:{port}/{database}', schema)


def _yes_no(question: str, default_no: bool = True) -> bool:
    """Tiny ``y/N`` prompt. ``default_no=True`` means an empty answer
    counts as no. Anything starting with ``y`` (case-insensitive) is
    yes; everything else is no."""
    suffix = '[y/N]' if default_no else '[Y/n]'
    raw = input(f'{question} {suffix}: ').strip().lower()
    if not raw:
        return not default_no
    return raw.startswith('y')


def _ensure_db_args(args: argparse.Namespace) -> None:
    """If ``--db`` was not given, walk the user through the connection
    interactively. Mutates ``args`` in place. Output dir, schema
    ownership and write surface are also asked interactively so the
    prompt is the single place a fresh user has to look."""
    interactive = not args.db
    if interactive:
        dsn, schema = _prompt_db_url()
        args.db = dsn
        if schema and not args.schema:
            args.schema = schema

    from urllib.parse import urlparse
    parsed = urlparse(args.db)
    dbname = parsed.path.lstrip('/') or 'project'
    default_out = f'./{dbname}'

    if interactive and not args.output:
        args.output = _prompt('Output directory', default_out)
    elif not args.output:
        args.output = default_out

    if interactive:
        # Two follow-up questions — defaults stay safe (managed=False,
        # read-only). The user only sees ``yes`` if they explicitly
        # type ``y``.
        if not args.django_managed:
            args.django_managed = _yes_no(
                'Let Django manage the schema (makemigrations + fake-initial on first run)?',
            )
        if not args.writable:
            args.writable = _yes_no(
                'Generate writable resources (POST/PATCH/DELETE)?',
            )


def _friendly_inspect(dsn: str, schema_name: str | None):
    """Wrap inspector exceptions in a clear message instead of leaking
    raw psycopg2 / pymysql tracebacks. The connection error story is
    the single most common reason people hit Ctrl-C and walk away."""
    from .inspector import inspect
    try:
        return inspect(dsn, schema=schema_name)
    except RuntimeError:
        raise  # composite-PK / driver-missing — already friendly
    except Exception as exc:
        scheme = (dsn.split('://', 1)[0] if '://' in dsn else dsn).lower()
        hint = ''
        if 'auth' in str(exc).lower() or 'access denied' in str(exc).lower() or 'password' in str(exc).lower():
            hint = ' (check user/password in the URL).'
        elif 'host' in str(exc).lower() or 'connect' in str(exc).lower() or 'timed out' in str(exc).lower():
            hint = ' (check host/port — is the database reachable from this machine?).'
        elif 'unknown database' in str(exc).lower() or 'does not exist' in str(exc).lower():
            hint = ' (check the database name in the URL).'
        sys.stderr.write(
            f'error: could not introspect {scheme} database{hint}\n'
            f'  reason: {exc.__class__.__name__}: {exc}\n'
        )
        raise SystemExit(1)


def _cmd_introspect(args: argparse.Namespace) -> int:
    schema = _friendly_inspect(args.db, args.schema)
    payload = json.dumps(dataclasses.asdict(schema), indent=2, default=str)
    if args.output and args.output != '-':
        Path(args.output).write_text(payload)
        print(f'wrote {args.output} ({len(schema.tables)} tables)', file=sys.stderr)
    else:
        sys.stdout.write(payload + '\n')
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from .config import build_starter_config, dump_yaml
    introspection = json.loads(Path(args.input).read_text())
    config = build_starter_config(
        introspection,
        include=_split_csv(args.include),
        exclude=_split_csv(args.exclude),
        read_only=not args.writable,
        django_managed=args.django_managed,
    )
    text = dump_yaml(config)
    if args.output and args.output != '-':
        Path(args.output).write_text(text)
        print(f'wrote {args.output}', file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    from .config import load_yaml
    from .generator import generate
    config = load_yaml(Path(args.config).read_text())
    out = Path(args.output)
    schema = None
    if args.schema_json:
        schema = json.loads(Path(args.schema_json).read_text())
    written = generate(config, out, schema=schema)
    print(f'generated {len(written)} files in {out}', file=sys.stderr)
    return 0


def _creds_from_dsn(dsn: str) -> dict:
    """Decode the DSN typed at the prompt into the env-var shape the
    settings template expects. Stays a plain dict to avoid any extra
    coupling between the CLI and the templates."""
    from urllib.parse import urlparse, unquote
    parsed = urlparse(dsn)
    scheme = (parsed.scheme or '').lower()
    default_port = 5432 if scheme.startswith('post') else 3306
    return {
        'db_name': parsed.path.lstrip('/'),
        'db_host': parsed.hostname or 'localhost',
        'db_port': parsed.port or default_port,
        'db_user': unquote(parsed.username or ''),
        'db_password': unquote(parsed.password or ''),
    }


def _cmd_init(args: argparse.Namespace) -> int:
    from .config import build_starter_config
    from .generator import generate, pick_sample
    _ensure_db_args(args)
    schema = _friendly_inspect(args.db, args.schema)
    introspection = dataclasses.asdict(schema)
    config = build_starter_config(
        introspection,
        include=_split_csv(args.include),
        exclude=_split_csv(args.exclude),
        read_only=not args.writable,
        django_managed=args.django_managed,
    )
    exposed = sum(1 for t in config.tables.values() if t.expose)
    out = Path(args.output)
    written = generate(config, out, schema=introspection, creds=_creds_from_dsn(args.db))
    print(
        f'introspected {len(schema.tables)} tables, exposed {exposed}, '
        f'generated {len(written)} files in {out}',
        file=sys.stderr,
    )
    _print_next_steps(out, pick_sample(config, schema=introspection))
    return 0


def _print_next_steps(out: Path, sample: dict | None) -> None:
    """Mirror the README's "Try it" section so the user can copy-paste
    a working request without opening the generated docs."""
    cd_target = out if out.is_absolute() else Path('./') / out
    print('\nnext steps:', file=sys.stderr)
    print(f'  cd {cd_target}', file=sys.stderr)
    print('  ./run.sh                              # or: python manage.py runserver', file=sys.stderr)
    if sample:
        print('', file=sys.stderr)
        print(f'  # REST — list rows from `{sample["rest_path"]}`:', file=sys.stderr)
        print(f'  curl -s http://localhost:8000/{sample["rest_path"]} | jq', file=sys.stderr)
        print('', file=sys.stderr)
        print('  # MCP — call the matching tool:', file=sys.stderr)
        print('  curl -s -X POST http://localhost:8000/mcp \\', file=sys.stderr)
        print("    -H 'Content-Type: application/json' \\", file=sys.stderr)
        print(
            f'    -d \'{{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            f'"params":{{"name":"{sample["mcp_tool"]}","arguments":{{}}}}}}\' | jq',
            file=sys.stderr,
        )
    print(
        '\nno X-Api-Key needed — DEFAULT_AUTHENTICATED=False on the demo project.',
        file=sys.stderr,
    )
    print(f'see {cd_target}/README.md for full docs.', file=sys.stderr)


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    """Args shared by ``init`` and ``config`` for shaping the table set
    and the project flavour."""
    p.add_argument(
        '--include',
        help='Comma-separated glob patterns. Only matching tables are exposed.',
    )
    p.add_argument(
        '--exclude',
        help='Comma-separated glob patterns to exclude even if --include matches.',
    )
    p.add_argument(
        '--writable', action='store_true',
        help='Generate resources with full CRUD (POST/PATCH/DELETE). '
             'Default is read-only — flip this when you want write verbs.',
    )
    p.add_argument(
        '--django-managed', action='store_true',
        help='Hand schema ownership to Django: drop ``managed = False`` '
             'and seed migrations via ``makemigrations`` + '
             '``migrate --fake-initial`` on first run. Default keeps '
             'the database external — Django reads, never writes DDL.',
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='0-mcp', description='0-mcp project generator.')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_int = sub.add_parser('introspect', help='Read a database, emit introspection.json.')
    p_int.add_argument('--db', required=True, help='Database URL (postgres://, mysql://, ...).')
    p_int.add_argument('--schema', help='Schema name (Postgres only; defaults to "public").')
    p_int.add_argument('--output', '-o', help='Output file (default: stdout).')
    p_int.set_defaults(func=_cmd_introspect)

    p_cfg = sub.add_parser('config', help='Build a starter config.yaml from introspection.json.')
    p_cfg.add_argument('input', help='Path to introspection.json.')
    p_cfg.add_argument('--output', '-o', help='Output file (default: stdout).')
    _add_filter_args(p_cfg)
    p_cfg.set_defaults(func=_cmd_config)

    p_gen = sub.add_parser('generate', help='Render config.yaml into a project tree.')
    p_gen.add_argument('config', help='Path to config.yaml.')
    p_gen.add_argument('--output', '-o', required=True, help='Project output directory.')
    p_gen.add_argument('--schema-json', help='Path to introspection.json (for full model emission).')
    p_gen.set_defaults(func=_cmd_generate)

    p_init = sub.add_parser('init', help='introspect → config → generate, in one shot.')
    p_init.add_argument('--db', help='Database URL. When omitted, prompts interactively.')
    p_init.add_argument('--schema', help='Schema name (Postgres only).')
    p_init.add_argument('--output', '-o', help='Project output directory (default: ./<dbname>).')
    _add_filter_args(p_init)
    p_init.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
