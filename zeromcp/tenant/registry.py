"""Facade around tenant DB connection plumbing.

Centralizes everything that touches `connections.databases`, the Redis
cache of connection dicts, and the alias convention. The rest of the
package consumes this facade — no other module should mutate
`connections.databases` directly.

Phase 1: same behavior as before, just encapsulated.
"""
import json

from django.db import connections

from ..exception import HTTPException
from ..redis_config import get_redis


class TenantConnectionRegistry:
    """Owns alias formatting, Redis-backed config cache, and the
    `connections.databases` registration."""

    def __init__(self, *, db_prefix, default_database, account_model_getter):
        self._db_prefix = db_prefix
        self._default_database = default_database
        # account_model is resolved lazily — apps may not be ready at import.
        self._account_model_getter = account_model_getter

    # ── alias / cache key ────────────────────────────────────────────

    def make_alias(self, account_id):
        if not account_id:
            return 'default'
        return f'{self._db_prefix}_{account_id}'

    def _cache_key(self, account_id):
        return f'{self._db_prefix}:connections:{account_id}'

    # ── connection dict construction ────────────────────────────────

    def _build_connection(self, alias, account):
        return {
            'ATOMIC_REQUESTS': False,
            'ENGINE': 'django.db.backends.mysql',
            'NAME': alias,
            'HOST': account.db.host,
            'USER': account.db.user,
            'PASSWORD': account.db.password,
            'CONN_MAX_AGE': 0,
            'CONN_HEALTH_CHECKS': False,
            'TIME_ZONE': None,
            'PORT': '',
            'AUTOCOMMIT': True,
            'OPTIONS': {
                'use_unicode': True,
                'charset': 'utf8mb4',
                'connect_timeout': 120,
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES', innodb_strict_mode=1",
            },
        }

    # ── Redis cache ─────────────────────────────────────────────────

    async def get_cached_connection(self, account_id):
        raw = await get_redis().get(self._cache_key(account_id))
        if raw:
            return json.loads(raw)
        return None

    async def cache_connection(self, account_id, connection):
        await get_redis().set(self._cache_key(account_id), json.dumps(connection))

    # ── connections.databases registration ──────────────────────────

    def register_connection(self, alias, connection):
        connections.databases[alias] = connection

    def is_registered(self, alias):
        return alias in connections.databases

    # ── high-level API ──────────────────────────────────────────────

    async def get_or_create(self, account_id, account=None):
        """Return ``(alias, connection)`` for ``account_id``.

        Tries Redis cache first, then falls back to building from
        ``account`` (fetched if not provided). Always caches the result
        and registers the alias in ``connections.databases``.
        """
        alias = self.make_alias(account_id)
        cached = await self.get_cached_connection(account_id)
        if cached is not None:
            connection = cached
        else:
            if account is None:
                model = self._account_model_getter()
                account = await model.objects.filter(
                    id=account_id,
                ).select_related('db').afirst()
            if account is None:
                raise HTTPException(400, 'Missing account')
            connection = self._build_connection(alias, account)
            await self.cache_connection(account_id, connection)

        self.register_connection(alias, connection)
        return alias, connection

    # ── default-connection swap (script-only) ───────────────────────

    async def set_default_connection(self, account_id):
        _, connection = await self.get_or_create(account_id)
        connections.databases['default'] = connection

    def restore_default_connection(self):
        connections.databases['default'] = self._default_database
