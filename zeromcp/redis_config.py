import asyncio
import os
import weakref

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

def _resolve_redis_env():
    """Read REDIS_SERVER / REDIS_DB lazily.

    Importing the framework should not require Redis to be configured —
    `manage.py check`, build tooling, and CI containers without Redis
    were crashing at import time. We resolve only when a client is
    actually requested.
    """
    server = os.environ.get('REDIS_SERVER')
    db = os.environ.get('REDIS_DB')
    if not server or db is None:
        raise RuntimeError(
            'REDIS_SERVER and REDIS_DB environment variables are required '
            'to use the 0-mcp Redis client.'
        )
    return server, int(db)


# Backwards-compatible module attributes — populated on first
# ``get_redis()``. None until then so that bare ``from .redis_config
# import REDIS_SERVER`` works for static-analysis but raising callers
# only blow up when they really need Redis.
REDIS_SERVER = None
REDIS_DB = None

try:
    from settings.env import REDIS_PREFIX
except ImportError:
    REDIS_PREFIX = os.environ.get('REDIS_PREFIX', '')

KEY_PREFIX = f'{REDIS_PREFIX}:' if REDIS_PREFIX else ''


# `redis.asyncio.Redis` ties its connection futures to the event loop
# it was created on. ASGI servers + ``async_to_sync`` bridges spin up
# multiple loops in the same process — a single process-global client
# crashes those calls with ``RuntimeError: got Future attached to a
# different loop``. We cache one client per loop in a WeakKeyDictionary
# so entries auto-clean when the loop is GC'd.
_clients = weakref.WeakKeyDictionary()

# Fallback for callers without a running loop (rare; sync contexts that
# manually drive the event loop). Created lazily.
_loopless_client = None


# Tests monkeypatch this attribute to inject a fake (e.g. fakeredis).
# When set, ``get_redis()`` returns it unconditionally. None in
# production, where per-loop caching takes over.
_redis_client = None


def get_redis():
    global _loopless_client, REDIS_SERVER, REDIS_DB

    # Test injection hook
    if _redis_client is not None:
        return _redis_client

    server, db = _resolve_redis_env()
    REDIS_SERVER, REDIS_DB = server, db

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if _loopless_client is None:
            _loopless_client = aioredis.Redis(
                host=server,
                db=db,
                decode_responses=True,
            )
        return _loopless_client

    client = _clients.get(loop)
    if client is None:
        client = aioredis.Redis(
            host=server,
            db=db,
            decode_responses=True,
        )
        try:
            _clients[loop] = client
        except TypeError:
            # Custom loop without weakref support — fall back to
            # returning a fresh client without caching.
            pass
    return client


_REDIS_TOO_OLD_MSG = (
    "0-mcp requires Redis 6.2 or newer — the framework uses GETEX for "
    "sliding session TTLs. Detected an older Redis that returned "
    "'unknown command GETEX'.\n"
    "Upgrade options:\n"
    "  • RHEL/Oracle Linux 8: `sudo dnf module reset redis -y && "
    "sudo dnf module enable redis:6 -y && sudo dnf install -y redis`\n"
    "  • Ubuntu/Debian:        `curl -fsSL https://packages.redis.io/gpg | "
    "sudo gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && "
    "echo \"deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] "
    "https://packages.redis.io/deb $(lsb_release -cs) main\" | "
    "sudo tee /etc/apt/sources.list.d/redis.list && "
    "sudo apt-get update && sudo apt-get install -y redis`\n"
    "  • Docker:               `docker run -d --name redis -p 6379:6379 "
    "redis:7-alpine`\n"
    "  • macOS (Homebrew):     `brew install redis`\n"
    "Then restart the app."
)


async def getex(client, key, ex):
    """``client.getex(key, ex=ex)`` with a friendly error on Redis < 6.2.

    The raw ``ResponseError('unknown command \\'GETEX\\'')`` from older
    Redis builds is cryptic — users hit it in production and spend hours
    diagnosing. We translate it once, here, and let everything else
    propagate untouched.
    """
    try:
        return await client.getex(key, ex=ex)
    except ResponseError as exc:
        msg = str(exc).lower()
        if 'unknown command' in msg and 'getex' in msg:
            raise RuntimeError(_REDIS_TOO_OLD_MSG) from exc
        raise


# Cache hit/miss counters live in two places now:
#
#   {KEY_PREFIX}cache_stats:hits        global hit counter   (INCR)
#   {KEY_PREFIX}cache_stats:misses      global miss counter  (INCR)
#   {KEY_PREFIX}cache_stats:by_model    HASH keyed by ``hits:<label>`` /
#                                       ``misses:<label>``     (HINCRBY)
#
# Pre-0.30 deploys used per-model keys (``cache_stats:hits:<label>``) and
# discovered them via ``SCAN``. With a large keyspace SCAN walked every
# key in the database, costing 10+ seconds for a single ``cachestats``
# call. The HASH variant reads everything in one ``HGETALL`` (O(M) where
# M = number of distinct models). Counters reset across the upgrade —
# they were observability-only.
_BY_MODEL_KEY = f'{KEY_PREFIX}cache_stats:by_model'


def cache_stats_field(outcome, label):
    """Hash field for ``cache_stats:by_model``. ``outcome`` is ``hits`` or
    ``misses``; ``label`` is the model's ``app.model`` lowercased name."""
    return f'{outcome}:{label}'


async def get_cache_stats():
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.get(f'{KEY_PREFIX}cache_stats:hits')
    pipe.get(f'{KEY_PREFIX}cache_stats:misses')
    pipe.hgetall(_BY_MODEL_KEY)
    raw_hits, raw_misses, raw_by_model = await pipe.execute()

    hits = int(raw_hits or 0)
    misses = int(raw_misses or 0)
    total = hits + misses
    ratio = hits / total if total else 0.0

    by_model = {}
    for field, value in (raw_by_model or {}).items():
        if ':' not in field:
            continue
        kind, label = field.split(':', 1)
        if kind not in ('hits', 'misses'):
            continue
        bucket = by_model.setdefault(label, {'hits': 0, 'misses': 0})
        bucket[kind] = int(value)

    for label, bucket in by_model.items():
        bucket['total'] = bucket['hits'] + bucket['misses']
        bucket['ratio'] = (
            bucket['hits'] / bucket['total'] if bucket['total'] else 0.0
        )

    return {
        'hits': hits,
        'misses': misses,
        'total': total,
        'ratio': ratio,
        'by_model': by_model,
    }


async def reset_cache_stats():
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.delete(f'{KEY_PREFIX}cache_stats:hits')
    pipe.delete(f'{KEY_PREFIX}cache_stats:misses')
    pipe.delete(_BY_MODEL_KEY)
    await pipe.execute()
