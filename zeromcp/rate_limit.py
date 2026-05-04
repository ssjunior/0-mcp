import logging
import secrets
import time

from . import blocking
from .redis_config import KEY_PREFIX, get_redis

logger = logging.getLogger(__name__)


class RateLimit:
    """Async rate limiter backed by ``redis.asyncio``.

    Uses the per-loop client from ``redis_config.get_redis()`` so it
    shares a connection pool with the rest of the framework instead of
    opening a parallel synchronous one (which would block the event
    loop on every check)."""

    def current_milli_time(self):
        return int(round(time.time() * 1000))

    async def is_blocked(self, identifier, action=None):
        """Check whether the identifier is currently blocked.

        Shares the ``blocking`` namespace with :mod:`security` and
        :class:`BaseResource`, so a single block fences every code path.
        ``action`` is accepted for backwards compatibility but ignored.
        """
        redis = get_redis()
        return await blocking.is_blocked(redis, identifier)

    async def track_failure(self, identifier, action="login", max_attempts=5, block_duration=900000):
        """Track failures and block once max_attempts is exceeded.

        On block, writes through :mod:`blocking` so the same key is
        visible to ``SecurityMiddleware``/``BaseResource``.
        """
        redis = get_redis()
        failure_key = f"{KEY_PREFIX}failures:{action}:{identifier}"
        p = redis.pipeline()
        p.incr(failure_key)
        p.expire(failure_key, block_duration // 1000)
        count, _ = await p.execute()
        if count >= max_attempts:
            await blocking.block(
                redis, identifier,
                reason=f'{action}-failures',
                ttl=block_duration // 1000,
            )
            return {"blocked": True, "reason": "Too many failed attempts"}
        return {"blocked": False}

    async def track_pattern(self, identifier, action="abuse"):
        """Detect regular request patterns indicative of abuse.

        Reports the signal but does NOT auto-write a block key. Frontends
        retrying after a server-side error look identical to a bot when
        you only consider request timing — auto-blocking that traffic
        locked legitimate users out and was opaque to debug. Callers may
        decide to escalate based on the returned ``suspicious`` flag plus
        other signals (e.g. authenticated identity, response codes).
        """
        redis = get_redis()
        pattern_key = f"{KEY_PREFIX}rate_limit:pattern:{action}:{identifier}"
        now = self.current_milli_time()
        last_req = await redis.get(f"{pattern_key}:last")
        last_req = int(last_req) if last_req else now
        interval = now - last_req
        await redis.set(f"{pattern_key}:last", now)

        p = redis.pipeline()
        p.lpush(f"{pattern_key}:intervals", interval)
        p.ltrim(f"{pattern_key}:intervals", 0, 9)
        p.expire(f"{pattern_key}:intervals", 3600)
        await p.execute()

        intervals = await redis.lrange(f"{pattern_key}:intervals", 0, -1)
        if len(intervals) > 5:
            intervals = [int(i) for i in intervals]
            mean = sum(intervals) / len(intervals)
            variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
            if variance < 100:
                logger.warning(
                    'rate_limit: suspicious request pattern from %s '
                    '(action=%s, mean_interval_ms=%.0f, variance=%.2f)',
                    identifier, action, mean, variance,
                )
                return {"suspicious": True, "reason": "Regular request intervals"}
        return {"suspicious": False}

    async def check_limits(self, identifier, limits, limit_type=None):
        """Check rate and abuse limits."""
        redis = get_redis()
        action = limit_type if limit_type else "api"
        if await self.is_blocked(identifier):
            return {"rate_limited": True, "abuse": False, "blocked": True}

        if action in ["api", "login"]:
            # Pattern detection is recorded for observability but no
            # longer short-circuits to ``abuse=True``. Timing-only
            # signals caught legitimate retry traffic (frontends
            # retrying after server-side errors look identical to bots
            # when judged by intervals alone). Real abuse is caught by
            # the count thresholds below; pattern alone goes to logs.
            await self.track_pattern(identifier, "abuse")

        now = self.current_milli_time()
        type_to_check = action
        key_prefix = f"{KEY_PREFIX}rate_limit:{type_to_check}"

        limit_configs = limits.get(type_to_check, limits["api"])
        if not isinstance(limit_configs, list):
            limit_configs = [limit_configs]
        limit_abuse_configs = limits["abuse"]

        p = redis.pipeline()

        for config in limit_configs:
            key = f"{key_prefix}:{config['interval']}:{identifier}"
            p.zremrangebyscore(key, 0, now - config["interval"])
            p.zcard(key)
            p.zadd(key, {f"req_{now}": now})
            p.expire(key, int(config["interval"] / 1000) + 1)

        abuse_results = []
        for config in limit_abuse_configs:
            abuse_key = f"{KEY_PREFIX}rate_limit:abuse:{config['interval']}:{identifier}"
            p.zremrangebyscore(abuse_key, 0, now - config["interval"])
            p.zcard(abuse_key)
            p.zadd(abuse_key, {f"req_{now}": now})
            p.expire(abuse_key, int(config["interval"] / 1000) + 1)
            abuse_results.append(config["limit"])

        pipeline_results = await p.execute()

        is_rate_limited = False
        for i, config in enumerate(limit_configs):
            count_type = pipeline_results[i * 4 + 1]
            if count_type >= config["limit"]:
                is_rate_limited = True

        is_abuse = False
        for i, config_limit in enumerate(abuse_results):
            count_abuse = pipeline_results[(len(limit_configs) * 4) + (i * 4) + 1]
            if count_abuse >= config_limit:
                is_abuse = True

        return {
            "rate_limited": is_rate_limited,
            "abuse": is_abuse,
            "blocked": False
        }

    async def api_limited(self, identifier, limits):
        """Check rate limits for API requests."""
        return await self.check_limits(identifier, limits, "api")

    async def login_limited(self, identifier, limits):
        """Check rate limits for login attempts."""
        return await self.check_limits(identifier, limits, "login")

    async def check_bucket(self, bucket, key, limit, window_ms):
        """Generic single-bucket rate-limit check.

        Sliding window over a Redis sorted set. Each call records a hit
        and returns the count for the window. Used by:
          • declarative ``BaseResource.rate_limits`` (per-resource buckets,
            arbitrary keys — e.g. ``email``, ``user_id``);
          • the credential-path auto-defense (per-IP, narrow window
            applied to ``/forgot``, ``/change_password``, ``/signup``).
        Independent of the ``api/login/abuse`` buckets handled by
        ``check_limits`` — does not write to the abuse namespace and
        does not auto-block.
        """
        redis = get_redis()
        now = self.current_milli_time()
        redis_key = f'{KEY_PREFIX}rate_limit:{bucket}:{window_ms}:{key}'
        # Unique member — multiple hits in the same millisecond would
        # otherwise collapse to a single sorted-set entry and undercount.
        member = f'req_{now}_{secrets.token_hex(4)}'
        p = redis.pipeline()
        p.zremrangebyscore(redis_key, 0, now - window_ms)
        p.zadd(redis_key, {member: now})
        p.zcard(redis_key)
        p.expire(redis_key, int(window_ms / 1000) + 1)
        results = await p.execute()
        count = results[2]
        return {
            'rate_limited': count > limit,
            'count': count,
            'limit': limit,
            'retry_after': max(int(window_ms / 1000), 1),
        }


RateLimiter = RateLimit()
