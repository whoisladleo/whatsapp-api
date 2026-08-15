import os
import redis

_redis_client = redis.from_url(
    os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
)


def check_and_increment(key: str, limit: int, window_seconds: int) -> bool:
    """
    Increments a counter for `key` and returns False once `limit` is exceeded
    within `window_seconds`. Fails open (returns True) if Redis is unreachable,
    so an outage never locks users out of auth entirely.
    """
    try:
        count = _redis_client.incr(key)
        if count == 1:
            _redis_client.expire(key, window_seconds)
        return count <= limit
    except redis.RedisError:
        return True
