import redis

from app.core.rate_limit import check_and_increment


def test_allows_under_limit():
    for _ in range(3):
        assert check_and_increment("k1", limit=3, window_seconds=60) is True


def test_blocks_once_limit_exceeded():
    for _ in range(3):
        check_and_increment("k2", limit=3, window_seconds=60)

    assert check_and_increment("k2", limit=3, window_seconds=60) is False


def test_keys_are_independent():
    for _ in range(3):
        check_and_increment("k3a", limit=3, window_seconds=60)

    # A different key should not be affected by k3a's count.
    assert check_and_increment("k3b", limit=3, window_seconds=60) is True


def test_fails_open_when_redis_unavailable(monkeypatch):
    import app.core.rate_limit as rate_limit_module

    class BrokenRedis:
        def incr(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())

    assert check_and_increment("k4", limit=1, window_seconds=60) is True


def test_explicit_fail_open_true_when_redis_unavailable(monkeypatch):
    import app.core.rate_limit as rate_limit_module

    class BrokenRedis:
        def incr(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())

    assert check_and_increment("k4b", limit=1, window_seconds=60, fail_open=True) is True


def test_fails_closed_when_redis_unavailable_and_fail_open_false(monkeypatch):
    import app.core.rate_limit as rate_limit_module

    class BrokenRedis:
        def incr(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())

    # Abuse-sensitive callers (OTP request/verify) deny during an outage
    # rather than unlimiting sends and guesses.
    assert check_and_increment("k5", limit=1, window_seconds=60, fail_open=False) is False


def test_fail_open_false_still_counts_normally():
    # The flag only changes the Redis-outage path, not normal counting.
    for _ in range(2):
        assert check_and_increment("k6", limit=2, window_seconds=60, fail_open=False) is True
    assert check_and_increment("k6", limit=2, window_seconds=60, fail_open=False) is False
