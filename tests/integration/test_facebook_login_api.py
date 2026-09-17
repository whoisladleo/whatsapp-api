import httpx
import jwt
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import User

TOKEN_URL_RE = r"https://graph\.facebook\.com/v25\.0/oauth/access_token"
ME_URL_RE = r"https://graph\.facebook\.com/v25\.0/me"


@pytest.fixture(autouse=True)
def _fb_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(settings, "FACEBOOK_LOGIN_REDIRECT_URI", "https://api.example.com/fb-cb")
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://dash.example.com")


def _mock_successful_exchange(facebook_id="fb_1", name="Jane Doe", email="jane@example.com"):
    respx.get(url__regex=TOKEN_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "fb-tok"})
    )
    respx.get(url__regex=ME_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": facebook_id, "name": name, "email": email})
    )


def test_start_facebook_login_returns_authorize_url_without_auth(client):
    resp = client.get("/api/v1/auth/facebook/start")

    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith(settings.FACEBOOK_OAUTH_AUTHORIZE_URL)


def test_callback_missing_code_redirects_to_error(client):
    resp = client.get("/api/v1/auth/facebook/callback", follow_redirects=False)

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?fb_error=1"


@respx.mock
def test_callback_success_creates_user_and_redirects_with_code(client, db_session):
    _mock_successful_exchange()

    resp = client.get(
        "/api/v1/auth/facebook/callback",
        params={"code": "auth-code", "state": "xyz"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://dash.example.com/login?fb_code=")

    user = db_session.query(User).filter(User.facebook_id == "fb_1").first()
    assert user is not None
    assert user.name == "Jane Doe"
    assert user.email == "jane@example.com"
    assert user.username is None

    login_code = location.split("fb_code=")[1]
    payload = jwt.decode(login_code, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert int(payload["sub"]) == user.id


@respx.mock
def test_callback_reuses_existing_user_on_repeat_login(client, db_session):
    existing = User(facebook_id="fb_1", name="Old Name", email="old@example.com")
    db_session.add(existing)
    db_session.commit()

    _mock_successful_exchange(name="New Name", email="new@example.com")

    client.get(
        "/api/v1/auth/facebook/callback",
        params={"code": "auth-code", "state": "xyz"},
        follow_redirects=False,
    )

    assert db_session.query(User).filter(User.facebook_id == "fb_1").count() == 1


@respx.mock
def test_callback_token_exchange_failure_redirects_to_error(client, db_session):
    respx.get(url__regex=TOKEN_URL_RE).mock(return_value=httpx.Response(400, json={}))

    resp = client.get(
        "/api/v1/auth/facebook/callback",
        params={"code": "auth-code", "state": "xyz"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/login?fb_error=1"
    assert db_session.query(User).count() == 0


def test_exchange_with_valid_code_returns_session_token(client, db_session):
    user = User(facebook_id="fb_2", name="Bob")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    login_code = create_access_token(user.id)

    resp = client.post("/api/v1/auth/facebook/exchange", json={"code": login_code})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    payload = jwt.decode(body["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert int(payload["sub"]) == user.id


def test_exchange_with_invalid_code_returns_401(client):
    resp = client.post("/api/v1/auth/facebook/exchange", json={"code": "not-a-real-token"})

    assert resp.status_code == 401


def test_exchange_with_expired_code_returns_401(client, db_session):
    from datetime import timedelta

    user = User(facebook_id="fb_3", name="Expired")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    expired_code = create_access_token(user.id, expires_delta=timedelta(minutes=-1))

    resp = client.post("/api/v1/auth/facebook/exchange", json={"code": expired_code})

    assert resp.status_code == 401
