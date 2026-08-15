from app.core.webhook_parser import WebhookParser


def _envelope(value: dict) -> dict:
    return {
        "entry": [
            {
                "id": "entry-1",
                "changes": [{"value": value, "field": "messages"}],
            }
        ]
    }


def _base_value(**overrides) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "255700000000",
            "phone_number_id": "PHONE123",
        },
        "contacts": [{"profile": {"name": "Test User"}, "wa_id": "255711111111"}],
    }
    value.update(overrides)
    return value


def test_parses_text_message():
    message = {
        "from": "255711111111",
        "id": "wamid.text1",
        "timestamp": "1234567890",
        "type": "text",
        "text": {"body": "Hello there"},
    }
    parsed = WebhookParser.parse_inbound(_envelope(_base_value(messages=[message])))

    assert len(parsed) == 1
    item = parsed[0]
    assert item["phone_number_id"] == "PHONE123"
    assert item["whatsapp_message_id"] == "wamid.text1"
    assert item["from_phone"] == "255711111111"
    assert item["type"] == "text"
    assert item["body"] == "Hello there"
    assert item["direction"] == "inbound"
    assert item["raw"] == message


def test_parses_button_reply():
    message = {
        "from": "255711111111",
        "id": "wamid.btn1",
        "type": "button",
        "button": {"text": "Yes please", "payload": "YES"},
    }
    parsed = WebhookParser.parse_inbound(_envelope(_base_value(messages=[message])))

    assert parsed[0]["type"] == "button"
    assert parsed[0]["body"] == "Yes please"


def test_parses_interactive_button_reply():
    message = {
        "from": "255711111111",
        "id": "wamid.int1",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "opt_1", "title": "Option One"},
        },
    }
    parsed = WebhookParser.parse_inbound(_envelope(_base_value(messages=[message])))

    assert parsed[0]["type"] == "interactive"
    assert parsed[0]["body"] == "Option One"


def test_parses_media_message_id_by_type():
    for msg_type in ["image", "document", "audio", "video"]:
        message = {
            "from": "255711111111",
            "id": f"wamid.{msg_type}1",
            "type": msg_type,
            msg_type: {"id": f"MEDIA-{msg_type}", "mime_type": "application/octet-stream"},
        }
        parsed = WebhookParser.parse_inbound(_envelope(_base_value(messages=[message])))

        assert parsed[0]["type"] == msg_type
        assert parsed[0]["body"] == f"MEDIA-{msg_type}"


def test_parses_status_update():
    status = {
        "id": "wamid.status1",
        "status": "delivered",
        "timestamp": "1234567890",
        "recipient_id": "255711111111",
    }
    parsed = WebhookParser.parse_inbound(
        _envelope(_base_value(statuses=[status]))
    )

    assert len(parsed) == 1
    item = parsed[0]
    assert item["phone_number_id"] == "PHONE123"
    assert item["whatsapp_message_id"] == "wamid.status1"
    assert item["status"] == "delivered"
    assert item["direction"] == "outbound"
    assert item["event"] == "status_update"
    assert item["raw"] == status


def test_parses_multiple_entries_and_changes():
    data = {
        "entry": [
            {
                "id": "e1",
                "changes": [
                    {"value": _base_value(messages=[{
                        "from": "255711111111", "id": "m1", "type": "text",
                        "text": {"body": "hi"},
                    }])},
                ],
            },
            {
                "id": "e2",
                "changes": [
                    {"value": _base_value(
                        metadata={"display_phone_number": "x", "phone_number_id": "PHONE456"},
                        statuses=[{"id": "s1", "status": "read"}],
                    )},
                ],
            },
        ]
    }
    parsed = WebhookParser.parse_inbound(data)

    assert len(parsed) == 2
    assert parsed[0]["phone_number_id"] == "PHONE123"
    assert parsed[1]["phone_number_id"] == "PHONE456"
    assert parsed[1]["event"] == "status_update"


def test_empty_payload_returns_empty_list():
    assert WebhookParser.parse_inbound({}) == []
    assert WebhookParser.parse_inbound({"entry": []}) == []
