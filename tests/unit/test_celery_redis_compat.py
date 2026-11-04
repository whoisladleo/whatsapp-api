"""Celery/kombu/redis-pin compatibility (#138).

kombu>=5.7 requires redis-py>=6.1 at broker-connect, but only as a runtime
VersionMismatch check — pip can't see it, so a fresh image build floated
kombu to 5.7.0 while redis stayed pinned at 5.2.1 and crash-looped the
worker with green CI. This runs kombu's own gate (no broker needed, the
check precedes connecting) so the next float fails here instead of prod.
"""


def test_installed_kombu_accepts_installed_redis():
    from kombu.transport.redis import Channel

    channel = Channel.__new__(Channel)
    channel.global_keyprefix = None
    assert callable(channel._get_client())
