import httpx
import pytest
import respx

from app.core.config import settings
from app.services import instagram_oauth
from app.services.instagram_oauth import InstagramOAuthError

SHORT_TOKEN_URL_RE = r"https://api\.instagram\.com/oauth/access_token$"
LONG_TOKEN_URL_RE = r"https://graph\.instagram\.com/access_token"
ME_URL_RE = r"https://graph\.instagram\.com/me\?"
SUBSCRIBE_URL_RE = r"https://graph\.instagram\.com/me/subscribed_apps"


@pytest.fixture(autouse=True)
def _ig_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(settings, "INSTAGRAM_REDIRECT_URI", "https://api.example.com/cb")
    monkeypatch.setattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "https://api.example.com/login-cb")


def test_build_authorize_url_includes_client_id_redirect_and_scopes():
    url = instagram_oauth.build_authorize_url("state123")

    assert url.startswith(settings.INSTAGRAM_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in url
    assert "state=state123" in url
    assert "instagram_business_basic" in url
    assert "instagram_business_manage_messages" in url
    # Stripped (#79): Meta denied manage_comments, and requesting it fails
    # the whole authorize for live merchants.
    assert "instagram_business_manage_comments" not in url


def test_build_authorize_url_fails_fast_on_missing_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_REDIRECT_URI", None)
    with pytest.raises(InstagramOAuthError, match="INSTAGRAM_REDIRECT_URI"):
        instagram_oauth.build_authorize_url("state123")

    monkeypatch.setattr(settings, "INSTAGRAM_REDIRECT_URI", "https://api.example.com/cb")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", None)
    with pytest.raises(InstagramOAuthError, match="INSTAGRAM_APP_ID"):
        instagram_oauth.build_authorize_url("state123")


def test_build_login_authorize_url_fails_fast_on_missing_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", None)
    with pytest.raises(InstagramOAuthError, match="INSTAGRAM_LOGIN_REDIRECT_URI"):
        instagram_oauth.build_login_authorize_url("state123")


def test_build_login_authorize_url_uses_login_redirect_and_dm_only_scopes():
    url = instagram_oauth.build_login_authorize_url("state123")

    assert url.startswith(settings.INSTAGRAM_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in url
    assert "state=state123" in url
    assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Flogin-cb" in url
    assert "instagram_business_basic" in url
    assert "instagram_business_manage_messages" in url
    # manage_comments is unapproved — requesting it fails the whole
    # authorize request for merchants outside the app's test roles.
    assert "instagram_business_manage_comments" not in url


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
async def test_exchange_code_for_short_lived_token_echoes_login_redirect_uri():
    route = respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "short-tok", "user_id": "1"})
    )

    token = await instagram_oauth.exchange_code_for_short_lived_token(
        "auth-code", redirect_uri="https://api.example.com/login-cb"
    )

    assert token == "short-tok"
    sent = route.calls.last.request.content.decode()
    assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Flogin-cb" in sent


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
async def test_refresh_long_lived_token_success():
    route = respx.get(url__regex=r"https://graph\.instagram\.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "refreshed-tok"})
    )

    token = await instagram_oauth.refresh_long_lived_token("old-tok")

    assert token == "refreshed-tok"
    assert route.calls.last.request.url.params["grant_type"] == "ig_refresh_token"


@respx.mock
async def test_refresh_long_lived_token_failure():
    respx.get(url__regex=r"https://graph\.instagram\.com/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"message": "expired"}})
    )

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.refresh_long_lived_token("dead-tok")


@respx.mock
async def test_fetch_account_identity_success():
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"id": "app_scoped_1", "user_id": "ig_user_1", "username": "shopowner"}
        )
    )

    ig_user_id, username = await instagram_oauth.fetch_account_identity("long-tok")

    assert ig_user_id == "ig_user_1"
    assert username == "shopowner"


@respx.mock
async def test_fetch_account_identity_failure():
    respx.get(url__regex=ME_URL_RE).mock(return_value=httpx.Response(400, json={"error": {}}))

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.fetch_account_identity("bad-tok")


@respx.mock
async def test_subscribe_to_webhooks_success():
    route = respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await instagram_oauth.subscribe_to_webhooks("long-tok")

    assert route.calls.last.request.url.params["subscribed_fields"] == "comments,messages"
    assert route.calls.last.request.url.params["access_token"] == "long-tok"


@respx.mock
async def test_subscribe_to_webhooks_messages_only():
    route = respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await instagram_oauth.subscribe_to_webhooks("long-tok", fields="messages")

    assert route.calls.last.request.url.params["subscribed_fields"] == "messages"


@respx.mock
async def test_subscribe_to_webhooks_failure():
    respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.subscribe_to_webhooks("bad-tok")


@respx.mock
async def test_unsubscribe_from_webhooks_success():
    route = respx.delete(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await instagram_oauth.unsubscribe_from_webhooks("long-tok")

    assert route.calls.last.request.url.params["access_token"] == "long-tok"


@respx.mock
async def test_unsubscribe_from_webhooks_failure():
    respx.delete(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )

    with pytest.raises(InstagramOAuthError):
        await instagram_oauth.unsubscribe_from_webhooks("bad-tok")
