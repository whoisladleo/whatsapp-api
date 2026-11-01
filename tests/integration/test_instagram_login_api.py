import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import InstagramAccount, User

SHORT_TOKEN_URL_RE = r"https://api\.instagram\.com/oauth/access_token$"
LONG_TOKEN_URL_RE = r"https://graph\.instagram\.com/access_token"
ME_URL_RE = r"https://graph\.instagram\.com/me\?"
SUBSCRIBE_URL_RE = r"https://graph\.instagram\.com/me/subscribed_apps"


@pytest.fixture(autouse=True)
def _ig_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(
        settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "https://api.example.com/login-cb"
    )
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://dash.example.com")


def _mock_successful_exchange(ig_user_id="ig_user_1", username="shopowner"):
    short_route = respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "short-tok"})
    )
    respx.get(url__regex=LONG_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "long-tok"})
    )
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"id": "app_scoped_id", "user_id": ig_user_id, "username": username}
        )
    )
    subscribe_route = respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    return short_route, subscribe_route


def _start_state(client) -> str:
    """Runs a real /instagram/start round-trip and returns its state token."""
    resp = client.get("/api/v1/auth/instagram/start")
    assert resp.status_code == 200
    authorize_url = resp.json()["authorize_url"]
    return parse_qs(urlparse(authorize_url).query)["state"][0]


def _login_code_from(location: str) -> str:
    return parse_qs(urlparse(location).query)["ig_code"][0]


def test_start_requires_no_auth_and_returns_login_authorize_url(client):
    resp = client.get("/api/v1/auth/instagram/start")

    assert resp.status_code == 200
    authorize_url = resp.json()["authorize_url"]
    assert authorize_url.startswith(settings.INSTAGRAM_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in authorize_url
    assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Flogin-cb" in authorize_url
    assert "instagram_business_manage_messages" in authorize_url
    assert "instagram_business_manage_comments" not in authorize_url

    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert payload["nonce"]
    assert "sub" not in payload


def test_start_missing_login_redirect_is_plain_500(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", None)

    resp = client.get("/api/v1/auth/instagram/start")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "INSTAGRAM_LOGIN_REDIRECT_URI is not configured."


def test_start_rate_limits_per_ip(client):
    for _ in range(30):
        assert client.get("/api/v1/auth/instagram/start").status_code == 200

    resp = client.get("/api/v1/auth/instagram/start")
    assert resp.status_code == 429


def test_callback_missing_code_redirects_to_error(client):
    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"state": _start_state(client)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?ig_error=1"


def test_callback_invalid_state_redirects_to_error(client):
    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "auth-code", "state": "not-a-real-token"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?ig_error=1"


def test_callback_rejects_a_session_token_as_state(client, auth_user):
    """State must carry a nonce — a valid JWT from another flow (e.g. a
    session token) must not drive the login callback."""
    user, _ = auth_user

    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "auth-code", "state": create_access_token(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?ig_error=1"


@respx.mock
def test_callback_new_instagram_creates_user_and_connects_channel(
    client, db_session
):
    short_route, subscribe_route = _mock_successful_exchange(
        ig_user_id="ig_new", username="newshop"
    )

    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "auth-code", "state": _start_state(client)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://dash.example.com/login?ig_code=")
    login_code = _login_code_from(location)

    # The token exchange echoes the login redirect URI, not the connect one.
    sent = short_route.calls.last.request.content.decode()
    assert "redirect_uri=https%3A%2F%2Fapi.example.com%2Flogin-cb" in sent

    user = db_session.query(User).one()
    assert user.username is None
    assert user.name == "newshop"

    account = db_session.query(InstagramAccount).one()
    assert account.ig_business_id == "ig_new"
    assert account.access_token == "long-tok"
    assert account.username == "newshop"
    assert account.user_id == user.id

    assert subscribe_route.called
    assert subscribe_route.calls.last.request.url.params["subscribed_fields"] == "messages"

    # The login code exchanges for a session JWT for that same user.
    exchanged = client.post("/api/v1/auth/instagram/exchange", json={"code": login_code})
    assert exchanged.status_code == 200
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert uuid.UUID(me.json()["id"]) == user.id


@respx.mock
def test_callback_known_instagram_logs_into_existing_owner(
    client, db_session, auth_user
):
    """The account link: an IG already connected under a phone-based user
    logs into that same user instead of creating a second account."""
    user, _ = auth_user
    db_session.add(
        InstagramAccount(
            ig_business_id="ig_linked", access_token="old-tok",
            username="linkedshop", user_id=user.id,
        )
    )
    db_session.commit()
    _mock_successful_exchange(ig_user_id="ig_linked", username="linkedshop")

    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "auth-code", "state": _start_state(client)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert db_session.query(User).count() == 1

    exchanged = client.post(
        "/api/v1/auth/instagram/exchange",
        json={"code": _login_code_from(resp.headers["location"])},
    )
    assert exchanged.status_code == 200
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"},
    )
    assert uuid.UUID(me.json()["id"]) == user.id

    # ... and the re-login refreshes the stored token.
    account = db_session.query(InstagramAccount).one()
    assert account.access_token == "long-tok"


@respx.mock
def test_callback_still_logs_in_even_if_webhook_subscription_fails(
    client, db_session
):
    respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "short-tok"})
    )
    respx.get(url__regex=LONG_TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "long-tok"})
    )
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(
            200, json={"id": "app_scoped_id", "user_id": "ig_user_1", "username": "shopowner"}
        )
    )
    respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )

    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "auth-code", "state": _start_state(client)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("https://dash.example.com/login?ig_code=")
    assert db_session.query(User).count() == 1
    assert db_session.query(InstagramAccount).count() == 1


@respx.mock
def test_callback_token_exchange_failure_redirects_to_error(client, db_session):
    respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(return_value=httpx.Response(400, json={}))

    resp = client.get(
        "/api/v1/auth/instagram/callback",
        params={"code": "bad-code", "state": _start_state(client)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?ig_error=1"
    assert db_session.query(User).count() == 0
    assert db_session.query(InstagramAccount).count() == 0


def test_exchange_invalid_code_is_unauthorized(client):
    resp = client.post(
        "/api/v1/auth/instagram/exchange", json={"code": "not-a-real-code"}
    )

    assert resp.status_code == 401


def test_exchange_expired_code_is_unauthorized(client, auth_user):
    user, _ = auth_user
    expired = jwt.encode(
        {"sub": str(user.id), "exp": datetime.utcnow() - timedelta(minutes=1)},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    resp = client.post("/api/v1/auth/instagram/exchange", json={"code": expired})

    assert resp.status_code == 401


def test_exchange_unknown_user_is_not_found(client):
    code = create_access_token(uuid.uuid4())

    resp = client.post("/api/v1/auth/instagram/exchange", json={"code": code})

    assert resp.status_code == 404


def test_exchange_success_returns_short_lived_access_and_refresh(client, auth_user):
    user, _ = auth_user
    code = create_access_token(user.id)

    resp = client.post("/api/v1/auth/instagram/exchange", json={"code": code})

    assert resp.status_code == 200
    body = resp.json()
    payload = jwt.decode(
        body["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert uuid.UUID(payload["sub"]) == user.id
    exp = datetime.utcfromtimestamp(payload["exp"])
    assert timedelta(minutes=55) < exp - datetime.utcnow() < timedelta(minutes=65)
    assert body["refresh_token"]


def test_exchange_rate_limits_per_code(client):
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/instagram/exchange", json={"code": "same-bad-code"}
            ).status_code
            == 401
        )

    resp = client.post(
        "/api/v1/auth/instagram/exchange", json={"code": "same-bad-code"}
    )
    assert resp.status_code == 429
