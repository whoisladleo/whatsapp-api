"""Schema datetime contract (#98): every datetime crossing the API
carries an explicit UTC offset, so consumers never parse server time as
local time again."""

from datetime import datetime, timezone

from app.schemas.common import UtcDateTime, _serialize_utc


def test_naive_utc_gets_offset():
    assert _serialize_utc(datetime(2026, 10, 31, 19, 38, 13)) == "2026-10-31T19:38:13+00:00"


def test_aware_datetime_passes_through():
    value = datetime(2026, 10, 31, 19, 38, 13, tzinfo=timezone.utc)
    assert _serialize_utc(value) == "2026-10-31T19:38:13+00:00"


def test_none_stays_none():
    assert _serialize_utc(None) is None


def test_message_schema_emits_offset():
    import uuid

    from app.schemas.whatsapp import MessageRead

    dumped = MessageRead(
        id=uuid.uuid4(),
        whatsapp_message_id=None,
        from_phone=None,
        to_phone=None,
        type="text",
        body="hi",
        direction="inbound",
        status="delivered",
        timestamp=datetime(2026, 10, 31, 19, 38, 13),
    ).model_dump(mode="json")

    assert dumped["timestamp"] == "2026-10-31T19:38:13+00:00"
    assert isinstance(UtcDateTime, object)
