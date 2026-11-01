import os
import redis

_redis_client = redis.from_url(
    os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
)


def check_and_increment(
    key: str, limit: int, window_seconds: int, *, fail_open: bool = True
) -> bool:
    """
    Increments a counter for `key` and returns False once `limit` is exceeded
    within `window_seconds`. If Redis is unreachable, returns `fail_open`:
    True (default) for limits where an outage must never lock users out,
    False for abuse-sensitive limits (OTP request/verify) where an outage
    must deny rather than unlimit sends and guesses.
    """
    try:
        count = _redis_client.incr(key)
        if count == 1:
            _redis_client.expire(key, window_seconds)
        return count <= limit
    except redis.RedisError:
        return fail_open
