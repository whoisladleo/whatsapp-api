import json
import uuid

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.provider import WhatsAppProvider
from app.db.models import Product


def _product(**overrides):
    kwargs = {
        "id": uuid.uuid4(),
        "slug": f"p-{uuid.uuid4().hex[:8]}",
        "name": "Shoes",
        "max_price": 50000.0,
        "min_price": 40000.0,
    }
    kwargs.update(overrides)
    return Product(**kwargs)

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
async def test_send_text_with_context_message_id(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.2"}]})
    )

    await provider.send_text("255711111111", "Sure!", context_message_id="wamid.original")

    sent = json.loads(route.calls.last.request.content)
    assert sent["context"] == {"message_id": "wamid.original"}


@respx.mock
async def test_send_text_without_context_message_id_omits_context(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.3"}]})
    )

    await provider.send_text("255711111111", "Hello!")

    sent = json.loads(route.calls.last.request.content)
    assert "context" not in sent


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


@respx.mock
async def test_send_product_message_payload(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.p1"}]})
    )

    result = await provider.send_product_message(
        "255711111111", "CAT123", "retailer-1",
        body="Still interested in the Shoes?", footer="Shop footer",
        context_message_id="wamid.original",
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "255711111111",
        "type": "interactive",
        "interactive": {
            "type": "product",
            "body": {"text": "Still interested in the Shoes?"},
            "footer": {"text": "Shop footer"},
            "action": {
                "catalog_id": "CAT123",
                "product_retailer_id": "retailer-1",
            },
        },
        "context": {"message_id": "wamid.original"},
    }
    assert result["messages"][0]["id"] == "wamid.p1"


@respx.mock
async def test_send_product_message_omits_optional_footer_and_context(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.p2"}]})
    )

    await provider.send_product_message("255711111111", "CAT123", "retailer-1", body="Hi")

    sent = json.loads(route.calls.last.request.content)
    assert "footer" not in sent["interactive"]
    assert "context" not in sent


@respx.mock
async def test_send_product_list_payload(provider):
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.pl1"}]})
    )

    await provider.send_product_list(
        "255711111111",
        "CAT123",
        sections=[
            {"title": "Shoes", "retailer_ids": ["r-1", "r-2"]},
            {"title": "Bags", "retailer_ids": ["r-3"]},
        ],
        body="Pick one:",
        header="Our catalog",
        footer="Reply with a name",
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["type"] == "interactive"
    assert sent["interactive"]["type"] == "product_list"
    assert sent["interactive"]["header"] == {"type": "text", "text": "Our catalog"}
    assert sent["interactive"]["body"] == {"text": "Pick one:"}
    assert sent["interactive"]["footer"] == {"text": "Reply with a name"}
    assert sent["interactive"]["action"] == {
        "catalog_id": "CAT123",
        "sections": [
            {
                "title": "Shoes",
                "product_items": [
                    {"product_retailer_id": "r-1"},
                    {"product_retailer_id": "r-2"},
                ],
            },
            {"title": "Bags", "product_items": [{"product_retailer_id": "r-3"}]},
        ],
    }


@respx.mock
async def test_send_product_card_or_text_sends_card_and_defaults_body(provider, monkeypatch):
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.card1"}]})
    )
    product = _product()

    result = await provider.send_product_card_or_text(
        "255711111111", product, "Still interested in the Shoes?"
    )

    assert route.call_count == 1  # one message, not card-plus-text
    sent = json.loads(route.calls.last.request.content)
    assert sent["interactive"]["type"] == "product"
    assert sent["interactive"]["action"] == {
        "catalog_id": "CAT123",
        "product_retailer_id": str(product.id),
    }
    # body carries the same line the text send would have
    assert sent["interactive"]["body"] == {"text": "Still interested in the Shoes?"}
    assert result["messages"][0]["id"] == "wamid.card1"


@respx.mock
async def test_send_product_card_or_text_falls_back_on_meta_error(provider, monkeypatch):
    """A Meta error payload (e.g. catalog not connected to this WABA) retries as text."""
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        side_effect=[
            httpx.Response(200, json={"error": {"message": "catalog not connected", "code": 133004}}),
            httpx.Response(200, json={"messages": [{"id": "wamid.text1"}]}),
        ]
    )

    result = await provider.send_product_card_or_text(
        "255711111111", _product(), "Still interested in the Shoes?"
    )

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    assert first["interactive"]["type"] == "product"  # card attempted first
    second = json.loads(route.calls[1].request.content)
    assert second == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "255711111111",
        "type": "text",
        "text": {"preview_url": False, "body": "Still interested in the Shoes?"},
    }
    assert result["messages"][0]["id"] == "wamid.text1"


@respx.mock
async def test_send_product_card_or_text_falls_back_on_transport_error(provider, monkeypatch):
    async def failing_card(*a, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(provider, "send_product_message", failing_card)
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.text2"}]})
    )

    result = await provider.send_product_card_or_text(
        "255711111111", _product(), "Fallback line"
    )

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["type"] == "text"
    assert sent["text"]["body"] == "Fallback line"
    assert result["messages"][0]["id"] == "wamid.text2"


@respx.mock
async def test_send_product_card_or_text_falls_back_when_catalog_unlinked(provider, monkeypatch):
    """CATALOG_ID unset (conftest default) means no card attempt at all — straight to text."""
    monkeypatch.setattr(settings, "CATALOG_ID", None)
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.text3"}]})
    )

    await provider.send_product_card_or_text(
        "255711111111", _product(), "Fallback line"
    )

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["type"] == "text"


@respx.mock
async def test_send_product_card_or_text_falls_back_when_retailer_missing(provider, monkeypatch):
    """A transient row with no id has no retailer mapping — straight to text."""
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.text4"}]})
    )

    await provider.send_product_card_or_text(
        "255711111111", _product(id=None), "Fallback line"
    )

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["type"] == "text"


@pytest.mark.parametrize(
    "card_response",
    [
        {},
        {"messaging_product": "whatsapp", "contacts": [{"input": "255711111111", "wa_id": "255711111111"}]},
    ],
    ids=["bare-empty", "contacts-only"],
)
@respx.mock
async def test_send_product_card_or_text_falls_back_on_bare_no_messages_response(
    provider, monkeypatch, card_response
):
    """A 200 card attempt with no error AND no messages key (bare {} or
    contacts-only) is not an accepted send — falls back to text."""
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        side_effect=[
            httpx.Response(200, json=card_response),
            httpx.Response(200, json={"messages": [{"id": "wamid.text5"}]}),
        ]
    )

    result = await provider.send_product_card_or_text(
        "255711111111", _product(), "Fallback line"
    )

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    assert first["interactive"]["type"] == "product"  # card attempted first
    second = json.loads(route.calls[1].request.content)
    assert second == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "255711111111",
        "type": "text",
        "text": {"preview_url": False, "body": "Fallback line"},
    }
    assert result["messages"][0]["id"] == "wamid.text5"


@respx.mock
async def test_send_product_card_or_text_propagates_fallback_transient(provider, monkeypatch):
    """Card rejected + fallback text hits a transient error → the error propagates
    to the caller (the nudge outer handler catches it), never swallowed."""
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        side_effect=[
            httpx.Response(200, json={"error": {"message": "catalog not connected", "code": 133004}}),
            httpx.ConnectError("network down"),
        ]
    )

    with pytest.raises(httpx.ConnectError):
        await provider.send_product_card_or_text(
            "255711111111", _product(), "Fallback line"
        )

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    assert first["interactive"]["type"] == "product"  # card attempted first
    second = json.loads(route.calls[1].request.content)
    assert second["type"] == "text"  # fallback attempted, then raised
