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
