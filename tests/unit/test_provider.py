import json

import httpx
import pytest
import respx

from app.core.provider import WhatsAppProvider

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
MEDIA_URL_RE = r"https://graph\.facebook\.com/.+/media$"


@pytest.fixture
def provider():
    return WhatsAppProvider(access_token="tok", phone_number_id="PHONE123")


@respx.mock
async def test_send_text_payload(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
    )

    result = await provider.send_text("255711111111", "Hello!")

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "255711111111",
        "type": "text",
        "text": {"preview_url": False, "body": "Hello!"},
    }
    assert result["messages"][0]["id"] == "wamid.1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_send_template_payload_without_components(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.2"}]})
    )

    await provider.send_template("255711111111", "hello_world")

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "messaging_product": "whatsapp",
        "to": "255711111111",
        "type": "template",
        "template": {"name": "hello_world", "language": {"code": "en_US"}},
    }


@respx.mock
async def test_send_template_payload_with_components(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.3"}]})
    )
    components = [{"type": "body", "parameters": [{"type": "text", "text": "Ada"}]}]

    await provider.send_template("255711111111", "greeting", components=components)

    sent = json.loads(route.calls.last.request.content)
    assert sent["template"]["components"] == components


@respx.mock
async def test_send_media_payload_uses_media_type_as_key(provider):
    """
    Regression test: send_media must build {"image": {"id": ...}} using the
    media_type value as a literal dict key, not `[media_type]` (which produces
    an unhashable list key and raises TypeError at runtime).
    """
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.4"}]})
    )

    await provider.send_media("255711111111", "MEDIA-ID-1", media_type="image")

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "messaging_product": "whatsapp",
        "to": "255711111111",
        "type": "image",
        "image": {"id": "MEDIA-ID-1"},
    }


@respx.mock
async def test_send_media_with_caption(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.5"}]})
    )

    await provider.send_media(
        "255711111111", "MEDIA-ID-2", media_type="document", caption="Invoice"
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["document"] == {"id": "MEDIA-ID-2", "caption": "Invoice"}


@respx.mock
async def test_upload_media_sends_multipart(provider):
    route = respx.post(url__regex=MEDIA_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": "MEDIA-ID-3"})
    )

    result = await provider.upload_media(b"filebytes", "image.png", "image/png")

    assert route.called
    assert result == {"id": "MEDIA-ID-3"}
    # multipart request should not carry the JSON content-type header used elsewhere
    assert "application/json" not in route.calls.last.request.headers.get("content-type", "")
