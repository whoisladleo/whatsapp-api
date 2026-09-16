import json

import httpx
import pytest
import respx

from app.core.instagram_provider import InstagramProvider

REPLIES_URL_RE = r"https://graph\.facebook\.com/.+/replies$"
MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


@pytest.fixture
def provider():
    return InstagramProvider(page_access_token="page-tok")


@respx.mock
async def test_reply_to_comment_payload(provider):
    route = respx.post(url__regex=REPLIES_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": "reply_123"})
    )

    result = await provider.reply_to_comment("comment_1", "Thanks for asking!")

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"message": "Thanks for asking!"}
    assert result["id"] == "reply_123"
    assert route.calls.last.request.headers["Authorization"] == "Bearer page-tok"


@respx.mock
async def test_send_private_reply_payload(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"recipient_id": "ig-scoped-1", "message_id": "msg_1"}
        )
    )

    result = await provider.send_private_reply(
        "ig_business_1", "comment_1", "Here's the price!"
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "recipient": {"comment_id": "comment_1"},
        "message": {"text": "Here's the price!"},
    }
    assert result["message_id"] == "msg_1"
    request_url = str(route.calls.last.request.url)
    assert request_url.endswith("/ig_business_1/messages")
