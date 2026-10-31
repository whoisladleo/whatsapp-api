from app.core.instagram_webhook_parser import InstagramWebhookParser


def _envelope(ig_business_id: str, *values: dict) -> dict:
    return {
        "entry": [
            {
                "id": ig_business_id,
                "changes": [{"value": value, "field": "comments"} for value in values],
            }
        ]
    }


def test_parses_comment_event():
    value = {
        "from": {"id": "igsid_1", "username": "commenter1"},
        "media": {"id": "media_1", "media_product_type": "REELS"},
        "id": "comment_1",
        "text": "how much?",
    }
    data = _envelope("ig_business_1", value)

    parsed = InstagramWebhookParser.parse_inbound(data)

    assert parsed == [
        {
            "ig_business_id": "ig_business_1",
            "comment_id": "comment_1",
            "media_id": "media_1",
            "from_username": "commenter1",
            "from_ig_scoped_id": "igsid_1",
            "body": "how much?",
            "raw": value,
        }
    ]


def test_ignores_non_comments_fields():
    data = {
        "entry": [
            {
                "id": "ig_business_1",
                "changes": [{"value": {"comment_id": "x"}, "field": "mentions"}],
            }
        ]
    }

    assert InstagramWebhookParser.parse_inbound(data) == []


def test_excludes_comments_authored_by_the_connected_account_itself():
    """
    The account's own public reply to a customer comment triggers another
    webhook event for that reply — without filtering this out, Amara would
    reply to her own reply, forever.
    """
    value = {
        "from": {"id": "ig_business_1", "username": "taskflow_os"},
        "media": {"id": "media_1"},
        "id": "reply_comment_1",
        "parent_id": "comment_1",
        "text": "Thanks for reaching out! I've sent you more info in your DMs",
    }
    data = _envelope("ig_business_1", value)

    assert InstagramWebhookParser.parse_inbound(data) == []


def test_still_parses_a_real_customer_comment_alongside_a_self_comment():
    self_value = {
        "from": {"id": "ig_business_1", "username": "taskflow_os"},
        "media": {"id": "media_1"},
        "id": "reply_comment_1",
        "text": "Thanks!",
    }
    customer_value = {
        "from": {"id": "igsid_2", "username": "commenter2"},
        "media": {"id": "media_1"},
        "id": "comment_2",
        "text": "how much?",
    }
    data = _envelope("ig_business_1", self_value, customer_value)

    parsed = InstagramWebhookParser.parse_inbound(data)

    assert [p["comment_id"] for p in parsed] == ["comment_2"]


def test_parses_multiple_comments_across_entries():
    value1 = {"from": {"id": "a", "username": "u1"}, "media": {"id": "m1"}, "id": "c1", "text": "hi"}
    value2 = {"from": {"id": "b", "username": "u2"}, "media": {"id": "m2"}, "id": "c2", "text": "yo"}
    data = {
        "entry": [
            _envelope("ig_business_1", value1)["entry"][0],
            _envelope("ig_business_2", value2)["entry"][0],
        ]
    }

    parsed = InstagramWebhookParser.parse_inbound(data)

    assert [p["comment_id"] for p in parsed] == ["c1", "c2"]
    assert [p["ig_business_id"] for p in parsed] == ["ig_business_1", "ig_business_2"]


def _dm_envelope(ig_business_id: str, *events: dict) -> dict:
    return {"entry": [{"id": ig_business_id, "messaging": list(events)}]}


def test_parses_dm_event():
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "igsid_1"},
            "recipient": {"id": "ig_business_1"},
            "message": {"mid": "mid_1", "text": "is it still available?"},
        },
    )

    parsed = InstagramWebhookParser.parse_inbound_messages(data)

    assert parsed == [
        {
            "ig_business_id": "ig_business_1",
            "sender_id": "igsid_1",
            "ig_message_id": "mid_1",
            "body": "is it still available?",
        }
    ]


def test_dm_ignores_echoes_of_our_own_sent_messages():
    """
    Meta echoes back the connected account's own DM sends through the same
    webhook subscription (message.is_echo) — without filtering this out,
    Amara would treat her own reply as a new inbound message and reply to
    it again, forever.
    """
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "ig_business_1"},
            "recipient": {"id": "igsid_1"},
            "message": {"mid": "mid_2", "text": "It's 45000 TZS.", "is_echo": True},
        },
    )

    assert InstagramWebhookParser.parse_inbound_messages(data) == []


def test_dm_ignores_non_message_events():
    data = _dm_envelope(
        "ig_business_1",
        {"sender": {"id": "igsid_1"}, "recipient": {"id": "ig_business_1"}, "read": {"mid": "mid_1"}},
    )

    assert InstagramWebhookParser.parse_inbound_messages(data) == []


def test_dm_ignores_events_sent_by_the_connected_account_itself():
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "ig_business_1"},
            "recipient": {"id": "igsid_1"},
            "message": {"mid": "mid_3", "text": "Thanks!"},
        },
    )

    assert InstagramWebhookParser.parse_inbound_messages(data) == []


def test_parses_multiple_dms_across_entries():
    data = {
        "entry": [
            _dm_envelope(
                "ig_business_1",
                {"sender": {"id": "a"}, "message": {"mid": "m1", "text": "hi"}},
            )["entry"][0],
            _dm_envelope(
                "ig_business_2",
                {"sender": {"id": "b"}, "message": {"mid": "m2", "text": "yo"}},
            )["entry"][0],
        ]
    }

    parsed = InstagramWebhookParser.parse_inbound_messages(data)

    assert [p["ig_message_id"] for p in parsed] == ["m1", "m2"]
    assert [p["ig_business_id"] for p in parsed] == ["ig_business_1", "ig_business_2"]


def test_dm_parses_shared_post_attachments():
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "igsid_1"},
            "recipient": {"id": "ig_business_1"},
            "message": {
                "mid": "mid_share1",
                "attachments": [
                    {
                        "type": "share",
                        "payload": {"url": "https://www.instagram.com/p/ABC123/"},
                    }
                ],
            },
        },
    )

    parsed = InstagramWebhookParser.parse_inbound_messages(data)

    assert parsed == [
        {
            "ig_business_id": "ig_business_1",
            "sender_id": "igsid_1",
            "ig_message_id": "mid_share1",
            "body": "",
            "attachments": [{"type": "share", "url": "https://www.instagram.com/p/ABC123/"}],
        }
    ]


def test_dm_parses_photo_and_sticker_attachments():
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "igsid_1"},
            "recipient": {"id": "ig_business_1"},
            "message": {
                "mid": "mid_media1",
                "attachments": [
                    {
                        "type": "image",
                        "payload": {"url": "https://cdn.example/photo.jpg"},
                    },
                    {
                        "type": "image",
                        "payload": {"sticker_id": "12345"},
                    },
                ],
            },
        },
    )

    parsed = InstagramWebhookParser.parse_inbound_messages(data)

    assert parsed[0]["body"] == ""
    assert parsed[0]["attachments"] == [
        {"type": "image", "url": "https://cdn.example/photo.jpg"},
        {"type": "sticker"},
    ]


def test_dm_text_messages_carry_no_attachments_key():
    data = _dm_envelope(
        "ig_business_1",
        {
            "sender": {"id": "igsid_1"},
            "recipient": {"id": "ig_business_1"},
            "message": {"mid": "mid_2", "text": "hi"},
        },
    )

    parsed = InstagramWebhookParser.parse_inbound_messages(data)

    assert "attachments" not in parsed[0]
