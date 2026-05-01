"""Shared blocked-identifier helpers for edge security and app rate limit.

Single source of truth for the ``rate_limit:blocked:<identifier>`` Redis
key, so ``SecurityMiddleware`` and ``BaseResource`` agree on format and
TTL semantics.
"""

from .redis_config import KEY_PREFIX


def block_key(identifier):
    """Build the Redis key for a blocked identifier (typically an IP)."""
    return f'{KEY_PREFIX}rate_limit:blocked:{identifier}'


async def is_blocked(redis, identifier):
    """Return True when ``identifier`` is currently blocked."""
    return await redis.get(block_key(identifier)) is not None


async def block(redis, identifier, reason=None, ttl=86400):
    """Mark ``identifier`` as blocked for ``ttl`` seconds.

    ``reason`` is stored as the Redis value for later inspection via
    :func:`get_block_reason`. Falls back to ``'1'`` when not provided.
    """
    await redis.setex(block_key(identifier), ttl, reason or '1')


async def get_block_reason(redis, identifier):
    """Return the stored block reason, or ``None`` when not blocked."""
    return await redis.get(block_key(identifier))
