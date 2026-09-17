import httpx
import pytest
import respx

from app.core.config import settings
from app.services import facebook_oauth
from app.services.facebook_oauth import FacebookOAuthError

TOKEN_URL_RE = r"https://graph\.facebook\.com/v25\.0/oauth/access_token"
ME_URL_RE = r"https://graph\.facebook\.com/v25\.0/me"


@pytest.fixture(autouse=True)
def _fb_config(monkeypatch):
    monkeypatch.setattr(settings, "FACEBOOK_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(settings, "FACEBOOK_LOGIN_REDIRECT_URI", "https://api.example.com/fb-cb")


def test_build_authorize_url_includes_client_id_redirect_and_scopes():
    url = facebook_oauth.build_authorize_url("state123")

    assert url.startswith(settings.FACEBOOK_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in url
    assert "state=state123" in url
    assert "public_profile" in url


@respx.mock
async def test_exchange_code_for_token_success():
    route = respx.get(url__regex=TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "fb-tok", "expires_in": 5184000})
    )

    token = await facebook_oauth.exchange_code_for_token("auth-code")

    assert token == "fb-tok"
    assert route.calls.last.request.url.params["client_id"] == "APPID123"
    assert route.calls.last.request.url.params["code"] == "auth-code"


@respx.mock
async def test_exchange_code_for_token_failure():
    respx.get(url__regex=TOKEN_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad code"}})
    )

    with pytest.raises(FacebookOAuthError):
        await facebook_oauth.exchange_code_for_token("bad-code")


@respx.mock
async def test_fetch_profile_success():
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"id": "fb_1", "name": "Jane Doe", "email": "jane@example.com"}
        )
    )

    facebook_id, name, email = await facebook_oauth.fetch_profile("fb-tok")

    assert facebook_id == "fb_1"
    assert name == "Jane Doe"
    assert email == "jane@example.com"


@respx.mock
async def test_fetch_profile_failure():
    respx.get(url__regex=ME_URL_RE).mock(return_value=httpx.Response(400, json={"error": {}}))

    with pytest.raises(FacebookOAuthError):
        await facebook_oauth.fetch_profile("bad-tok")
