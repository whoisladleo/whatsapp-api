"""B2 vision product-match: stubbed-vision behavior tests.

All model calls here are stubbed — the live Muse vision probes are
throwaway scripts (see the B2 report), not committed tests.
"""

import httpx
import pytest
import respx

import app.services.vision_match as vision_match
from app.db.models import Product, User
from app.services.llm import LLMService


# --- Fakes ---


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeChatResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeChat:
    def __init__(self, handler):
        self.completions = _FakeCompletions(handler)


class _FakeOpenAIClient:
    """OpenAI-shaped stub: client.chat.completions.create(...)."""

    def __init__(self, handler):
        self.chat = _FakeChat(handler)


def _vision_handler(reply_text=None, captured=None, exc=None):
    async def handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        if exc is not None:
            raise exc
        return _FakeChatResponse(reply_text)

    return handler


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000310")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def catalog(db_session, merchant):
    shoes = Product(
        slug="vm-shoes", name="Red Sneakers", max_price=85000, min_price=70000,
        quantity=3, merchant_id=merchant.id,
        description="Bright red running sneakers.",
    )
    bag = Product(
        slug="vm-bag", name="Leather Handbag", max_price=120000, min_price=100000,
        quantity=2, merchant_id=merchant.id,
        description="Brown genuine-leather handbag.",
    )
    db_session.add_all([shoes, bag])
    db_session.commit()
    db_session.refresh(shoes)
    db_session.refresh(bag)
    return [shoes, bag]


def _media(**overrides):
    media = {
        "id": "media_1",
        "caption": "New kicks",
        "media_url": "https://cdn.example/photo.jpg",
        "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/abc/",
        "children": [],
    }
    media.update(overrides)
    return media


@pytest.fixture
def stubbed_download(monkeypatch):
    """Stub the server-side download; returns the calls list."""
    calls = []

    async def fake_download(url):
        calls.append(url)
        return "data:image/jpeg;base64,ZmFrZS1ieXRlcw=="

    monkeypatch.setattr(vision_match, "_download_image_as_data_url", fake_download)
    return calls


# --- match_post_to_product ---


async def test_match_returns_catalog_product(catalog, stubbed_download):
    captured = {}
    client = _FakeOpenAIClient(_vision_handler("Red Sneakers", captured=captured))

    result = await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    )

    assert result is not None
    assert result.name == "Red Sneakers"
    assert stubbed_download == ["https://cdn.example/photo.jpg"]
    # The model sees the image as a data URL plus the catalog (names,
    # descriptions, prices) — never a remote URL (Meta can't fetch those).
    content = captured["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "Red Sneakers" in content[0]["text"]
    assert "Bright red running sneakers." in content[0]["text"]
    assert "85,000" in content[0]["text"]
    assert "Leather Handbag" in content[0]["text"]


async def test_match_is_case_insensitive(catalog, stubbed_download):
    client = _FakeOpenAIClient(_vision_handler("red sneakers"))

    result = await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    )

    assert result is not None
    assert result.name == "Red Sneakers"


async def test_no_match_returns_none(catalog, stubbed_download):
    client = _FakeOpenAIClient(_vision_handler("NO_MATCH"))

    assert await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    ) is None


async def test_invented_product_name_returns_none(catalog, stubbed_download):
    """A confident-sounding but non-catalog name must never become context."""
    client = _FakeOpenAIClient(_vision_handler("Blue Suede Boots"))

    assert await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    ) is None


async def test_prose_instead_of_name_returns_none(catalog, stubbed_download):
    client = _FakeOpenAIClient(
        _vision_handler("The photo appears to show the Red Sneakers.")
    )

    assert await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    ) is None


async def test_model_error_returns_none(catalog, stubbed_download):
    client = _FakeOpenAIClient(
        _vision_handler(exc=RuntimeError("model API down"))
    )

    assert await vision_match.match_post_to_product(
        client=client, model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    ) is None


async def test_missing_media_url_skips_vision(catalog, stubbed_download):
    async def boom(**kwargs):
        raise AssertionError("vision must not be called without a media_url")

    result = await vision_match.match_post_to_product(
        client=_FakeOpenAIClient(boom), model="muse-spark-1.3",
        post_media=_media(media_url=None), products=catalog,
    )

    assert result is None
    assert stubbed_download == []


async def test_video_post_skips_vision(catalog, stubbed_download):
    """A VIDEO media_url is a clip, not usable vision input."""
    async def boom(**kwargs):
        raise AssertionError("vision must not be called for video posts")

    result = await vision_match.match_post_to_product(
        client=_FakeOpenAIClient(boom), model="muse-spark-1.3",
        post_media=_media(
            media_url="https://cdn.example/reel.mp4", media_type="VIDEO"
        ),
        products=catalog,
    )

    assert result is None
    assert stubbed_download == []


async def test_no_post_media_returns_none(catalog, stubbed_download):
    async def boom(**kwargs):
        raise AssertionError("vision must not be called without post_media")

    assert await vision_match.match_post_to_product(
        client=_FakeOpenAIClient(boom), model="muse-spark-1.3",
        post_media=None, products=catalog,
    ) is None
    assert stubbed_download == []


async def test_empty_catalog_returns_none(stubbed_download):
    async def boom(**kwargs):
        raise AssertionError("vision must not be called with no products")

    assert await vision_match.match_post_to_product(
        client=_FakeOpenAIClient(boom), model="muse-spark-1.3",
        post_media=_media(), products=[],
    ) is None
    assert stubbed_download == []


async def test_download_failure_returns_none_without_calling_vision(
    catalog, monkeypatch
):
    async def failing_download(url):
        return None

    async def boom(**kwargs):
        raise AssertionError("vision must not be called when download fails")

    monkeypatch.setattr(vision_match, "_download_image_as_data_url", failing_download)

    assert await vision_match.match_post_to_product(
        client=_FakeOpenAIClient(boom), model="muse-spark-1.3",
        post_media=_media(), products=catalog,
    ) is None


# --- _download_image_as_data_url ---


@respx.mock
async def test_download_returns_data_url():
    respx.get("https://cdn.example/photo.jpg").mock(
        return_value=httpx.Response(
            200, content=b"fake-bytes", headers={"content-type": "image/jpeg"}
        )
    )

    assert await vision_match._download_image_as_data_url(
        "https://cdn.example/photo.jpg"
    ) == "data:image/jpeg;base64,ZmFrZS1ieXRlcw=="


@respx.mock
async def test_download_non_200_returns_none():
    respx.get("https://cdn.example/photo.jpg").mock(
        return_value=httpx.Response(404, content=b"not found")
    )

    assert await vision_match._download_image_as_data_url(
        "https://cdn.example/photo.jpg"
    ) is None


@respx.mock
async def test_download_non_image_returns_none():
    respx.get("https://cdn.example/photo.jpg").mock(
        return_value=httpx.Response(
            200, content=b"<html>", headers={"content-type": "text/html"}
        )
    )

    assert await vision_match._download_image_as_data_url(
        "https://cdn.example/photo.jpg"
    ) is None


@respx.mock
async def test_download_oversize_returns_none(monkeypatch):
    monkeypatch.setattr(vision_match, "VISION_MAX_IMAGE_BYTES", 4)
    respx.get("https://cdn.example/photo.jpg").mock(
        return_value=httpx.Response(
            200, content=b"way too many bytes", headers={"content-type": "image/jpeg"}
        )
    )

    assert await vision_match._download_image_as_data_url(
        "https://cdn.example/photo.jpg"
    ) is None


@respx.mock
async def test_download_transport_error_returns_none():
    respx.get("https://cdn.example/photo.jpg").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    assert await vision_match._download_image_as_data_url(
        "https://cdn.example/photo.jpg"
    ) is None


# --- generate_comment_reply wiring ---


def _comment_service(provider_name, captured):
    service = LLMService(api_key=None, provider=provider_name)
    service.client = object()  # truthy so generate_comment_reply doesn't short-circuit

    async def stubbed_complete(*, system, messages, max_tokens):
        captured["system"] = system
        return (
            "PUBLIC: Thanks for asking! Sent you the details in DMs.\n"
            "PRIVATE: The Red Sneakers are TZS 85,000 and in stock."
        )

    service.provider.complete = stubbed_complete
    return service


def _stub_match(monkeypatch, matched):
    calls = []

    async def fake_match(*, client, model, post_media, products):
        calls.append(post_media)
        return matched

    monkeypatch.setattr("app.services.llm.match_post_to_product", fake_match)
    return calls


async def test_comment_reply_names_matched_product(
    db_session, merchant, catalog, monkeypatch
):
    """A confident vision match lands in the reply prompt as catalog context."""
    captured = {}
    service = _comment_service("muse", captured)
    calls = _stub_match(monkeypatch, catalog[0])

    result = await service.generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
        post_media=_media(),
    )

    assert calls == [_media()]
    assert result == {
        "public_reply": "Thanks for asking! Sent you the details in DMs.",
        "private_reply": "The Red Sneakers are TZS 85,000 and in stock.",
    }
    assert "shows this catalog item: Red Sneakers (Price TZS 85,000)" in captured["system"]


async def test_comment_reply_no_match_falls_back_exactly(
    db_session, merchant, catalog, monkeypatch
):
    """NO_MATCH (None) keeps the prompt byte-identical to the no-media one."""
    _stub_match(monkeypatch, None)

    with_media, without_media = {}, {}
    await _comment_service("muse", with_media).generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
        post_media=_media(),
    )
    await _comment_service("muse", without_media).generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
    )

    assert with_media["system"] == without_media["system"]


async def test_comment_reply_no_media_never_calls_vision(
    db_session, merchant, catalog, monkeypatch
):
    calls = _stub_match(monkeypatch, catalog[0])

    await _comment_service("muse", {}).generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
    )

    assert calls == []


async def test_comment_reply_vision_error_falls_back(
    db_session, merchant, catalog, monkeypatch
):
    """Even a raising vision step can't break the reply — it falls back."""

    async def raising_match(*, client, model, post_media, products):
        raise RuntimeError("vision exploded")

    monkeypatch.setattr("app.services.llm.match_post_to_product", raising_match)
    captured = {}

    result = await _comment_service("muse", captured).generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
        post_media=_media(),
    )

    assert result is not None
    assert "shows this catalog item" not in captured["system"]


async def test_comment_reply_claude_never_calls_vision(
    db_session, merchant, catalog, monkeypatch
):
    """The Claude path is untouched: vision never runs, prompt unchanged."""

    async def boom(*, client, model, post_media, products):
        raise AssertionError("Claude path must never call vision")

    monkeypatch.setattr("app.services.llm.match_post_to_product", boom)
    captured = {}
    service = _comment_service("claude", captured)

    result = await service.generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session,
        post_media=_media(),
    )

    assert result is not None
    assert "shows this catalog item" not in captured["system"]
