"""Tests for the ``zeromcp.gen`` submodule.

Inspector connection logic is exercised end-to-end against a real DB
in development; here we cover the deterministic, in-process bits:
naming/grouping rules, config building, YAML round-trip, CHECK-clause
parsing, and the full generate pipeline using a synthetic schema.
"""
from __future__ import annotations

import pytest

from zeromcp.gen.config import (
    Config,
    Project,
    TableConfig,
    build_starter_config,
    dump_yaml,
    load_yaml,
)
from zeromcp.gen.generator import (
    _build_fk_lookup,
    _group_by_module,
    _module_prefix,
    generate,
)
from zeromcp.gen.inspector.postgres import _pascal_case, _parse_choices_from_check


# ── Naming helpers ──────────────────────────────────────────────────


@pytest.mark.parametrize('raw,expected', [
    ('client', 'Client'),
    ('client_contact', 'ClientContact'),
    ('contract_billing_contact', 'ContractBillingContact'),
    ('invoice_invoicehistory', 'InvoiceHistory'),
    ('contract_contractdiscount', 'ContractDiscount'),
    ('auth_user_user_permissions', 'AuthUserPermissions'),
    ('order_history_log', 'OrderHistoryLog'),
])
def test_pascal_case_strips_duplicate_prefix(raw, expected):
    assert _pascal_case(raw) == expected


@pytest.mark.parametrize('db_table,expected_prefix', [
    ('client', 'client'),
    ('client_contact', 'client'),
    ('contract_item', 'contract'),
    ('Boleto', 'boleto'),
    ('', 'misc'),
    # Hyphen-named tables are rare but legal — first underscore segment
    # is taken, then unsafe chars are coerced to ``_``.
    ('weird-name', 'weird_name'),
])
def test_module_prefix(db_table, expected_prefix):
    assert _module_prefix(db_table) == expected_prefix


# ── CHECK-clause choice parsing ─────────────────────────────────────


def test_parse_choices_from_in_clause():
    clause = "(status_id IN (1, 2, 3))"
    assert _parse_choices_from_check(clause, 'status_id') == [
        (1, '1'), (2, '2'), (3, '3'),
    ]


def test_parse_choices_from_any_array_clause():
    clause = "type_id = ANY (ARRAY[1, 2, 3])"
    assert _parse_choices_from_check(clause, 'type_id') == [
        (1, '1'), (2, '2'), (3, '3'),
    ]


def test_parse_choices_returns_none_when_column_not_referenced():
    assert _parse_choices_from_check("foo IN (1,2)", 'bar') is None


def test_parse_choices_returns_none_for_complex_clauses():
    assert _parse_choices_from_check("amount > 0", 'amount') is None


# ── Config sensitivity / system detection ───────────────────────────


def _intro(tables):
    return {'backend': 'postgres', 'database': 'demo', 'tables': tables}


def test_build_starter_marks_password_token_secret_as_sensitive():
    cfg = build_starter_config(_intro([{
        'name': 'User',
        'db_table': 'user',
        'columns': [
            {'name': 'id'}, {'name': 'email'},
            {'name': 'password'}, {'name': 'api_key'},
            {'name': 'session_key'}, {'name': 'token_user'},
        ],
    }]))
    sensitive = cfg.tables['User'].sensitive
    assert 'password' in sensitive
    assert 'api_key' in sensitive
    assert 'session_key' in sensitive
    assert 'token_user' in sensitive
    assert 'email' not in sensitive
    assert 'id' not in sensitive


def test_build_starter_does_not_flag_force_password_reset():
    """Common false positive — a boolean flag, not a credential."""
    cfg = build_starter_config(_intro([{
        'name': 'User', 'db_table': 'user',
        'columns': [{'name': 'force_password_reset'}],
    }]))
    assert cfg.tables['User'].sensitive == []


def test_django_system_tables_default_to_hidden():
    cfg = build_starter_config(_intro([
        {'name': 'DjangoMigrations', 'db_table': 'django_migrations', 'columns': []},
        {'name': 'AuthUser', 'db_table': 'auth_user', 'columns': []},
        {'name': 'Client', 'db_table': 'client', 'columns': []},
    ]))
    assert cfg.tables['DjangoMigrations'].expose is False
    assert cfg.tables['AuthUser'].expose is False
    assert cfg.tables['Client'].expose is True


def test_include_glob_filters_to_whitelist():
    cfg = build_starter_config(_intro([
        {'name': 'Client', 'db_table': 'client', 'columns': []},
        {'name': 'Invoice', 'db_table': 'invoice', 'columns': []},
        {'name': 'Audit', 'db_table': 'audit_log', 'columns': []},
    ]), include=['client', 'invoice*'])
    assert cfg.tables['Client'].expose is True
    assert cfg.tables['Invoice'].expose is True
    assert cfg.tables['Audit'].expose is False


def test_exclude_wins_over_include():
    cfg = build_starter_config(_intro([
        {'name': 'Tmp', 'db_table': 'tmp_data', 'columns': []},
        {'name': 'Real', 'db_table': 'real_data', 'columns': []},
    ]), include=['*_data'], exclude=['tmp_*'])
    assert cfg.tables['Tmp'].expose is False
    assert cfg.tables['Real'].expose is True


def test_read_only_and_dev_mode_flow_through():
    cfg = build_starter_config(_intro([]), read_only=True, dev_mode=True)
    assert cfg.project.read_only is True
    assert cfg.project.dev_mode is True


# ── YAML round-trip ─────────────────────────────────────────────────


def test_yaml_round_trip_preserves_config():
    pytest.importorskip('yaml')
    cfg = Config(
        project=Project(
            name='demo', backend='postgres', database='demo',
            read_only=True, dev_mode=False,
        ),
        tables={
            'Client': TableConfig(expose=True, db_table='client', sensitive=['ssn']),
            'Audit': TableConfig(expose=False, db_table='audit_log'),
        },
    )
    text = dump_yaml(cfg)
    again = load_yaml(text)
    assert again.project.name == 'demo'
    assert again.project.read_only is True
    assert again.project.dev_mode is False
    assert again.tables['Client'].sensitive == ['ssn']
    assert again.tables['Audit'].expose is False


# ── Generator output ────────────────────────────────────────────────


def _synthetic_schema():
    """A two-table schema with one cross-module FK and one self FK,
    enough to exercise the interesting generator paths."""
    return {
        'backend': 'postgres', 'database': 'demo',
        'tables': [
            {
                'name': 'Client', 'db_table': 'client',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigAutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None, 'decimal_digits': None,
                     'default': None, 'fk_target': None, 'choices': None,
                     'comment': None, 'db_column': 'id'},
                    {'name': 'name', 'db_type': 'character varying',
                     'py_type': 'CharField', 'null': False, 'primary_key': False,
                     'unique': False, 'auto_increment': False, 'max_length': 191,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'name'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 1000,
            },
            {
                'name': 'User', 'db_table': 'user',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigAutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None, 'decimal_digits': None,
                     'default': None, 'fk_target': None, 'choices': None,
                     'comment': None, 'db_column': 'id'},
                    {'name': 'client_id', 'db_type': 'bigint',
                     'py_type': 'BigIntegerField', 'null': False,
                     'primary_key': False, 'unique': False, 'auto_increment': False,
                     'max_length': None, 'decimal_digits': None, 'default': None,
                     'fk_target': 'public.client.id', 'choices': None,
                     'comment': None, 'db_column': 'client_id'},
                    {'name': 'created_by_id', 'db_type': 'bigint',
                     'py_type': 'BigIntegerField', 'null': True,
                     'primary_key': False, 'unique': False, 'auto_increment': False,
                     'max_length': None, 'decimal_digits': None, 'default': None,
                     'fk_target': 'public.user.id', 'choices': None,
                     'comment': None, 'db_column': 'created_by_id'},
                    {'name': 'password', 'db_type': 'character varying',
                     'py_type': 'CharField', 'null': False, 'primary_key': False,
                     'unique': False, 'auto_increment': False, 'max_length': 128,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'password'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 50,
            },
        ],
        'relationships': [],
    }


def test_group_by_module_buckets_by_db_prefix():
    cfg = build_starter_config(_synthetic_schema())
    schema_tables = {t['name']: t for t in _synthetic_schema()['tables']}
    groups = _group_by_module(cfg, schema_tables)
    assert sorted(groups.keys()) == ['client', 'user']
    assert [c[0] for c in groups['client']] == ['Client']
    assert [c[0] for c in groups['user']] == ['User']


def test_fk_lookup_qualifies_targets():
    cfg = build_starter_config(_synthetic_schema())
    lookup = _build_fk_lookup(cfg)
    assert lookup['client'] == 'client.Client'
    assert lookup['user'] == 'user.User'


def test_generate_writes_complete_project(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema)
    written = generate(cfg, tmp_path, schema=schema)
    paths = {str(p.relative_to(tmp_path)) for p in written}
    # Top-level scaffolding present.
    assert 'manage.py' in paths
    assert 'asgi.py' in paths
    assert 'requirements.txt' in paths
    assert 'settings/settings.py' in paths
    assert 'router/urls.py' in paths
    assert 'router/endpoints.py' in paths
    # One module per prefix. No ``apps.py`` — Django auto-creates the
    # AppConfig from the dotted path in INSTALLED_APPS.
    assert 'modules/client/models.py' in paths
    assert 'modules/client/resources.py' in paths
    assert 'modules/user/models.py' in paths
    assert 'modules/user/resources.py' in paths
    assert not any(p.endswith('/apps.py') for p in paths)


def test_generate_emits_self_fk_for_intra_table_relations(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    user_models = (tmp_path / 'modules' / 'user' / 'models.py').read_text()
    # ``user.User.created_by`` points back to the same model — must use
    # the literal ``'self'`` form, not a string class reference.
    assert "'self'" in user_models
    assert "created_by = models.ForeignKey('self'" in user_models


def test_generate_qualifies_cross_module_fks(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    user_models = (tmp_path / 'modules' / 'user' / 'models.py').read_text()
    # ``user.User.client`` points to a different module — must use the
    # ``'<app>.<Model>'`` form so Django resolves cross-app.
    assert "'client.Client'" in user_models


def test_generate_propagates_sensitive_fields(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    user_resources = (tmp_path / 'modules' / 'user' / 'resources.py').read_text()
    assert "sensitive_fields = ['password']" in user_resources


def test_generate_read_only_emits_settings_gate(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema, read_only=True)
    generate(cfg, tmp_path, schema=schema)
    client_resources = (tmp_path / 'modules' / 'client' / 'resources.py').read_text()
    # Per-resource ``allowed_methods`` no longer emitted — gate is global.
    assert 'allowed_methods' not in client_resources
    settings_py = (tmp_path / 'settings' / 'settings.py').read_text()
    assert "'READ_ONLY': True" in settings_py


def test_generate_writable_omits_read_only_setting(tmp_path):
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema, read_only=False)
    generate(cfg, tmp_path, schema=schema)
    settings_py = (tmp_path / 'settings' / 'settings.py').read_text()
    assert 'READ_ONLY' not in settings_py


def test_generated_project_is_demo_friendly_by_default(tmp_path):
    """Generated projects always boot — DEBUG=true, MCP exposed without
    auth, public docs. The README documents the hardening checklist."""
    pytest.importorskip('jinja2')
    schema = _synthetic_schema()
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    urls_py = (tmp_path / 'router' / 'urls.py').read_text()
    # MCP route forwards ``authenticated=False`` via mcp_kwargs so the
    # demo project exposes /mcp without auth.
    assert "'authenticated': False" in urls_py
    assert 'mcp=True' in urls_py
    assert 'docs_public=True' in urls_py
    env_example = (tmp_path / '.env.example').read_text()
    assert 'DEBUG=true' in env_example


def test_generate_skips_redundant_bigautofield_id(tmp_path):
    """``id = BigAutoField(primary_key=True)`` is redundant because
    Django's ``DEFAULT_AUTO_FIELD`` (BigAutoField since 3.2) auto-adds
    it. The generator omits the line to keep models clean."""
    pytest.importorskip('jinja2')
    schema = {
        'backend': 'mysql', 'database': 'demo',
        'tables': [
            {
                'name': 'File', 'db_table': 'file',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigAutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'id'},
                    {'name': 'path', 'db_type': 'varchar', 'py_type': 'CharField',
                     'null': False, 'primary_key': False, 'unique': False,
                     'auto_increment': False, 'max_length': 255,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'path'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
        ],
        'relationships': [],
    }
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    models_py = (tmp_path / 'modules' / 'file' / 'models.py').read_text()
    assert 'BigAutoField' not in models_py
    # Other columns still emitted.
    assert 'path = models.CharField' in models_py


def test_generate_keeps_smallautofield_and_autofield_explicit(tmp_path):
    """Only BigAutoField named ``id`` matches the project default —
    smaller auto fields must stay explicit so the requested width
    isn't silently widened to BigAutoField by Django."""
    pytest.importorskip('jinja2')
    schema = {
        'backend': 'mysql', 'database': 'demo',
        'tables': [
            {
                'name': 'Tiny', 'db_table': 'tiny',
                'columns': [
                    {'name': 'id', 'db_type': 'int', 'py_type': 'AutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'id'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
        ],
        'relationships': [],
    }
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    models_py = (tmp_path / 'modules' / 'tiny' / 'models.py').read_text()
    assert 'id = models.AutoField(primary_key=True)' in models_py


def test_generate_emits_primary_key_for_shared_pk_fk(tmp_path):
    """A column that is BOTH primary key and foreign key (the
    shared-primary-key pattern, common in 1:1 ``profile`` extending
    ``user``) must keep ``primary_key=True`` on the ForeignKey;
    otherwise Django auto-creates an implicit ``id`` and the model
    stops reflecting the real table."""
    pytest.importorskip('jinja2')
    schema = {
        'backend': 'mysql', 'database': 'demo',
        'tables': [
            {
                'name': 'User', 'db_table': 'user',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigAutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'id'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
            {
                'name': 'Profile', 'db_table': 'profile',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigIntegerField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': False, 'max_length': None,
                     'decimal_digits': None, 'default': None,
                     'fk_target': 'public.user.id',
                     'choices': None, 'comment': None, 'db_column': 'id'},
                    {'name': 'bio', 'db_type': 'text', 'py_type': 'TextField',
                     'null': True, 'primary_key': False, 'unique': False,
                     'auto_increment': False, 'max_length': None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'bio'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
        ],
        'relationships': [],
    }
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    models_py = (tmp_path / 'modules' / 'profile' / 'models.py').read_text()
    # The FK column must declare itself the primary key on the model.
    assert 'models.ForeignKey' in models_py
    assert 'primary_key=True' in models_py
    # And keep the db_column so writes hit the right column.
    assert "db_column='id'" in models_py


def test_generate_skips_primary_key_on_composite_pk_fk(tmp_path):
    """When the table has a composite primary key that includes a FK
    column, the individual FK column must NOT carry ``primary_key=True``
    — the ``CompositePrimaryKey`` declaration at the top of the class
    is what marks the PK. ``primary_key=True`` on individual columns
    would conflict with the composite declaration."""
    pytest.importorskip('jinja2')
    schema = {
        'backend': 'mysql', 'database': 'demo',
        'tables': [
            {
                'name': 'Tag', 'db_table': 'tag',
                'columns': [
                    {'name': 'id', 'db_type': 'bigint', 'py_type': 'BigAutoField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': True, 'max_length': None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'id'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
            {
                'name': 'PostTag', 'db_table': 'post_tag',
                'columns': [
                    {'name': 'post_id', 'db_type': 'bigint', 'py_type': 'BigIntegerField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': False, 'max_length': None,
                     'decimal_digits': None, 'default': None,
                     'fk_target': 'public.post.id',
                     'choices': None, 'comment': None, 'db_column': 'post_id'},
                    {'name': 'tag_id', 'db_type': 'bigint', 'py_type': 'BigIntegerField',
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': False, 'max_length': None,
                     'decimal_digits': None, 'default': None,
                     'fk_target': 'public.tag.id',
                     'choices': None, 'comment': None, 'db_column': 'tag_id'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
        ],
        'relationships': [],
    }
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    models_py = (tmp_path / 'modules' / 'post' / 'models.py').read_text()
    # CompositePrimaryKey at top of class.
    assert 'CompositePrimaryKey' in models_py
    # Individual FK columns must NOT carry primary_key=True.
    fk_lines = [
        line for line in models_py.splitlines()
        if 'models.ForeignKey' in line
    ]
    assert fk_lines, 'expected ForeignKey declarations'
    for line in fk_lines:
        assert 'primary_key=True' not in line, (
            f'composite PK column should not have primary_key=True: {line}'
        )


@pytest.mark.parametrize('py_type', ['IntegerField', 'BigIntegerField', 'CharField'])
def test_generate_emits_primary_key_for_non_auto_pk(tmp_path, py_type):
    """Legacy schemas with non-auto PKs (``id INT PRIMARY KEY``,
    ``id BIGINT PRIMARY KEY`` without auto_increment, or a ``CHAR``
    PK) must still get ``primary_key=True`` on the field — otherwise
    Django's system check rejects with ``models.E004``."""
    pytest.importorskip('jinja2')
    schema = {
        'backend': 'mysql', 'database': 'demo',
        'tables': [
            {
                'name': 'File', 'db_table': 'file',
                'columns': [
                    {'name': 'id', 'db_type': 'int', 'py_type': py_type,
                     'null': False, 'primary_key': True, 'unique': False,
                     'auto_increment': False,
                     'max_length': 64 if py_type == 'CharField' else None,
                     'decimal_digits': None, 'default': None, 'fk_target': None,
                     'choices': None, 'comment': None, 'db_column': 'id'},
                ],
                'indexes': [], 'unique_together': [],
                'comment': None, 'row_estimate': 0,
            },
        ],
        'relationships': [],
    }
    cfg = build_starter_config(schema)
    generate(cfg, tmp_path, schema=schema)
    models_py = (tmp_path / 'modules' / 'file' / 'models.py').read_text()
    assert f'id = models.{py_type}(' in models_py
    assert 'primary_key=True' in models_py
