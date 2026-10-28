import httpx
import pytest
import respx

import app.tasks.instagram_comment_processing as task_module
from app.core.instagram_provider import InstagramProvider
from app.db.models import InstagramAccount, InstagramComment, User

MEDIA_URL_RE = r"https://graph\.instagram\.com/.+/media_1"
EXPECTED_FIELDS = "caption,media_url,media_type,permalink,children{media_url,media_type}"


@pytest.fixture
def provider():
    return InstagramProvider(access_token="ig-tok")


@respx.mock
async def test_get_media_photo_post(provider):
    route = respx.get(url__regex=MEDIA_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "media_1",
                "caption": "New kicks just landed",
                "media_url": "https://cdn.example/photo.jpg",
                "media_type": "IMAGE",
                "permalink": "https://www.instagram.com/p/abc/",
            },
        )
    )

    result = await provider.get_media("media_1")

    assert route.called
    assert route.calls.last.request.url.params["fields"] == EXPECTED_FIELDS
    assert (
        route.calls.last.request.headers["Authorization"]
        == provider.headers["Authorization"]
    )
    assert result == {
        "id": "media_1",
        "caption": "New kicks just landed",
        "media_url": "https://cdn.example/photo.jpg",
        "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/abc/",
        "children": [],
    }


@respx.mock
async def test_get_media_carousel_picks_first_image_child(provider):
    # Carousel nodes carry no media_url of their own — the rule is first
    # IMAGE child (here the second child, after a video).
    route = respx.get(url__regex=MEDIA_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "media_1",
                "caption": "Swipe for more",
                "media_type": "CAROUSEL_ALBUM",
                "permalink": "https://www.instagram.com/p/car/",
                "children": {
                    "data": [
                        {"media_type": "VIDEO", "media_url": "https://cdn.example/clip.mp4", "id": "c1"},
                        {"media_type": "IMAGE", "media_url": "https://cdn.example/slide2.jpg", "id": "c2"},
                        {"media_type": "IMAGE", "media_url": "https://cdn.example/slide3.jpg", "id": "c3"},
                    ]
                },
            },
        )
    )

    result = await provider.get_media("media_1")

    assert route.called
    assert result["media_url"] == "https://cdn.example/slide2.jpg"
    assert result["media_type"] == "IMAGE"
    assert result["caption"] == "Swipe for more"
    assert result["permalink"] == "https://www.instagram.com/p/car/"
    assert len(result["children"]) == 3


@respx.mock
async def test_get_media_video_post(provider):
    respx.get(url__regex=MEDIA_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "media_1",
                "caption": "Watch this",
                "media_url": "https://cdn.example/reel.mp4",
                "media_type": "VIDEO",
                "permalink": "https://www.instagram.com/p/vid/",
            },
        )
    )

    result = await provider.get_media("media_1")

    assert result["media_url"] == "https://cdn.example/reel.mp4"
    assert result["media_type"] == "VIDEO"


@respx.mock
async def test_get_media_deleted_media_returns_none(provider):
    # Deleted/private media comes back as an error payload, not an
    # exception — still just None.
    respx.get(url__regex=MEDIA_URL_RE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "Unsupported get request.",
                    "type": "GraphMethodException",
                    "code": 100,
                }
            },
        )
    )

    assert await provider.get_media("media_1") is None


@respx.mock
async def test_get_media_transport_error_returns_none(provider):
    respx.get(url__regex=MEDIA_URL_RE).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    assert await provider.get_media("media_1") is None


async def test_generate_comment_reply_ignores_post_media_for_now(db_session):
    """B1 threads post_media into generate_comment_reply but reply behavior
    is unchanged until B2 consumes it."""
    from app.services.llm import LLMService

    owner = User(username="255700000902")
    db_session.add(owner)
    db_session.commit()

    captured = {}
    service = LLMService(api_key=None, provider="claude")
    service.client = object()  # truthy so generate_comment_reply doesn't short-circuit

    async def stubbed_complete(*, system, messages, max_tokens):
        captured["system"] = system
        return "PUBLIC: Thanks!\nPRIVATE: It's 45000 TZS."

    service.provider.complete = stubbed_complete
    media = {"id": "media_1", "media_url": "https://cdn.example/photo.jpg"}

    result = await service.generate_comment_reply(
        "how much?", merchant_id=owner.id, db=db_session, post_media=media
    )

    assert result == {"public_reply": "Thanks!", "private_reply": "It's 45000 TZS."}
    assert "photo.jpg" not in captured["system"]


# --- Threading: task -> get_media -> generate_comment_reply ---


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    monkeypatch.setattr(task_module, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000901")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_1",
        access_token="ig-tok",
        username="shopowner",
        ai_enabled=True,
        user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def comment(db_session, account):
    c = InstagramComment(
        account_id=account.id,
        comment_id="comment_1",
        media_id="media_1",
        from_username="commenter1",
        from_ig_scoped_id="igsid_1",
        body="how much?",
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


class _MediaFakeProvider:
    def __init__(self, access_token, media=None):
        self.access_token = access_token
        self.media = media
        self.get_media_calls = []

    async def get_media(self, media_id):
        self.get_media_calls.append(media_id)
        return self.media

    async def reply_to_comment(self, comment_id, message):
        return {"id": "reply_1"}

    async def send_private_reply(self, ig_business_id, comment_id, message):
        return {"message_id": "msg_1"}


def test_task_fetches_parent_media_and_passes_it_to_reply(
    db_session, account, comment, monkeypatch
):
    media = {
        "id": "media_1",
        "caption": "New kicks",
        "media_url": "https://cdn.example/photo.jpg",
        "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/abc/",
        "children": [],
    }
    fake_provider = _MediaFakeProvider("ig-tok", media=media)
    monkeypatch.setattr(
        task_module, "InstagramProvider", lambda access_token: fake_provider
    )

    async def fake_generate_comment_reply(**kwargs):
        fake_generate_comment_reply.called_with = kwargs
        return {"public_reply": "Sent you a DM!", "private_reply": "It's 45000 TZS."}

    fake_generate_comment_reply.called_with = None

    class FakeLLMService:
        generate_comment_reply = staticmethod(fake_generate_comment_reply)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())

    result = task_module.process_inbound_comment(account.id, comment.id)

    assert result == "Processed."
    assert fake_provider.get_media_calls == ["media_1"]
    assert fake_generate_comment_reply.called_with["post_media"] == media


def test_task_falls_back_to_none_when_media_fetch_fails(
    db_session, account, comment, monkeypatch
):
    class _FailingProvider(_MediaFakeProvider):
        async def get_media(self, media_id):
            raise RuntimeError("Graph API down")

    monkeypatch.setattr(
        task_module, "InstagramProvider", lambda access_token: _FailingProvider("ig-tok")
    )

    async def fake_generate_comment_reply(**kwargs):
        fake_generate_comment_reply.called_with = kwargs
        return {"public_reply": "Sent you a DM!", "private_reply": "It's 45000 TZS."}

    fake_generate_comment_reply.called_with = None

    class FakeLLMService:
        generate_comment_reply = staticmethod(fake_generate_comment_reply)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())

    result = task_module.process_inbound_comment(account.id, comment.id)

    assert result == "Processed."
    assert fake_generate_comment_reply.called_with["post_media"] is None
