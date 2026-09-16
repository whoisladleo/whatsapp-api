import httpx
import pytest
import respx

from app.core.config import settings
from app.services import instagram_oauth
from app.services.instagram_oauth import InstagramOAuthError

SHORT_TOKEN_URL_RE = r"https://api\.instagram\.com/oauth/access_token$"
LONG_TOKEN_URL_RE = r"https://graph\.instagram\.com/access_token"
ME_URL_RE = r"https://graph\.instagram\.com/me"


@pytest.fixture(autouse=True)
def _ig_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(settings, "INSTAGRAM_REDIRECT_URI", "https://api.example.com/cb")


def test_build_authorize_url_includes_client_id_redirect_and_scopes():
    url = instagram_oauth.build_authorize_url("state123")

    assert url.startswith(settings.INSTAGRAM_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in url
    assert "state=state123" in url
    assert "instagram_business_manage_comments" in url
    assert "instagram_business_manage_messages" in url


@respx.mock
async def test_exchange_code_for_short_lived_token_success():
    route = respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "short-tok", "user_id": "1"})
    )

    token = await instagram_oauth.exchange_code_for_short_lived_token("auth-code")

    assert token == "short-tok"
    sent = route.calls.last.request.content.decode()
    assert "client_id=APPID123" in sent
    assert "code=auth-code" in sent


@respx.mock
async def test_exchange_code_for_short_lived_token_failure():
    respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
        return_value=httpx.Response(400, json={"error_message": "bad code"})
    )

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.exchange_code_for_short_lived_token("bad-code")


@respx.mock
async def test_exchange_for_long_lived_token_success():
    route = respx.get(url__regex=LONG_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "long-tok", "expires_in": 5184000})
    )

    token = await instagram_oauth.exchange_for_long_lived_token("short-tok")

    assert token == "long-tok"
    assert route.calls.last.request.url.params["grant_type"] == "ig_exchange_token"


@respx.mock
async def test_exchange_for_long_lived_token_failure():
    respx.get(url__regex=LONG_TOKEN_URL_RE).mock(return_value=httpx.Response(400, json={}))

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.exchange_for_long_lived_token("short-tok")


@respx.mock
async def test_fetch_account_identity_success():
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": "ig_user_1", "username": "shopowner"})
    )

    ig_user_id, username = await instagram_oauth.fetch_account_identity("long-tok")

    assert ig_user_id == "ig_user_1"
    assert username == "shopowner"


@respx.mock
async def test_fetch_account_identity_failure():
    respx.get(url__regex=ME_URL_RE).mock(return_value=httpx.Response(400, json={"error": {}}))

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.fetch_account_identity("bad-tok")
