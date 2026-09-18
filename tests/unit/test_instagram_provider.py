import json

import httpx
import pytest
import respx

from app.core.instagram_provider import InstagramProvider

REPLIES_URL_RE = r"https://graph\.instagram\.com/.+/replies$"
MESSAGES_URL_RE = r"https://graph\.instagram\.com/.+/messages$"
PROFILE_URL_RE = r"https://graph\.instagram\.com/.+/igsid_1"


@pytest.fixture
def provider():
    return InstagramProvider(access_token="ig-tok")


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
    assert route.calls.last.request.headers["Authorization"] == "Bearer ig-tok"


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


@respx.mock
async def test_send_direct_message_payload(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"recipient_id": "ig-scoped-1", "message_id": "msg_2"}
        )
    )

    result = await provider.send_direct_message(
        "ig_business_1", "igsid_1", "It's 45000 TZS."
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "recipient": {"id": "igsid_1"},
        "message": {"text": "It's 45000 TZS."},
    }
    assert result["message_id"] == "msg_2"
    request_url = str(route.calls.last.request.url)
    assert request_url.endswith("/ig_business_1/messages")


@respx.mock
async def test_get_user_profile_returns_name_and_username(provider):
    route = respx.get(url__regex=PROFILE_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": "igsid_1", "name": "Jane Doe", "username": "janedoe"})
    )

    result = await provider.get_user_profile("igsid_1")

    assert route.called
    assert result["username"] == "janedoe"
    assert result["name"] == "Jane Doe"
    assert route.calls.last.request.url.params["fields"] == "name,username"
    assert route.calls.last.request.headers["Authorization"] == "Bearer ig-tok"


@respx.mock
async def test_get_user_profile_returns_error_payload_as_is(provider):
    respx.get(url__regex=PROFILE_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "Unsupported request"}})
    )

    result = await provider.get_user_profile("igsid_1")

    assert "error" in result
