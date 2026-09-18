import json

import httpx
import jwt
import pytest
import respx

from app.core.config import settings
from app.db.models import User

PHONE = "255712345678"


def _mock_send_text():
    """Mocks the outbound Graph API call the OTP background task makes."""
    return respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.otp"}]})
    )


@respx.mock
def test_request_otp_creates_user_and_sends_message(client, db_session):
    route = _mock_send_text()

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    user = db_session.query(User).filter(User.username == PHONE).first()
    assert user is not None
    assert user.otp_code is not None
    assert len(user.otp_code) == 6
    assert route.called


@respx.mock
def test_request_otp_reuses_existing_user(client, db_session):
    _mock_send_text()
    existing = User(username=PHONE)
    db_session.add(existing)
    db_session.commit()

    client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert db_session.query(User).filter(User.username == PHONE).count() == 1


@respx.mock
def test_request_and_verify_otp_with_different_phone_formats_match_same_user(
    client, db_session
):
    """
    A merchant who requests an OTP typing their number in local format
    (leading 0) must still be able to verify with the international format
    (255...) WhatsApp itself would supply, and vice versa — both must
    resolve to the same User row via normalize_phone, not create two.
    """
    _mock_send_text()

    client.post("/api/v1/auth/request-otp", json={"username": "0712345678"})
    user = db_session.query(User).filter(User.username == PHONE).first()
    assert user is not None
    otp_code = user.otp_code

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": otp_code}
    )

    assert resp.status_code == 200
    assert db_session.query(User).count() == 1


@respx.mock
def test_verify_otp_success_returns_token(client, db_session):
    _mock_send_text()
    client.post("/api/v1/auth/request-otp", json={"username": PHONE})
    user = db_session.query(User).filter(User.username == PHONE).first()
    otp_code = user.otp_code

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": otp_code}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    payload = jwt.decode(
        body["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert int(payload["sub"]) == user.id

    db_session.refresh(user)
    assert user.otp_code is None
    assert user.otp_expires_at is None


@respx.mock
def test_verify_otp_wrong_code(client, db_session):
    _mock_send_text()
    client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": "000000"}
    )

    assert resp.status_code == 401


def test_verify_otp_unknown_user(client):
    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": "255700000000", "otp_code": "123456"}
    )

    assert resp.status_code == 404


@respx.mock
def test_verify_otp_expired_code(client, db_session):
    from datetime import datetime, timedelta

    _mock_send_text()
    client.post("/api/v1/auth/request-otp", json={"username": PHONE})
    user = db_session.query(User).filter(User.username == PHONE).first()
    user.otp_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": user.otp_code}
    )

    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


@respx.mock
def test_request_otp_still_succeeds_when_whatsapp_send_fails(client, db_session):
    # The background OTP-send task swallows send errors and logs them; the
    # request-otp response itself must still succeed.
    respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        side_effect=httpx.ConnectError("boom")
    )

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    user = db_session.query(User).filter(User.username == PHONE).first()
    assert user.otp_code is not None


@respx.mock
def test_request_otp_rate_limited_after_three_requests(client):
    _mock_send_text()

    for _ in range(3):
        resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})
        assert resp.status_code == 200

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 429


@respx.mock
def test_verify_otp_rate_limited_after_five_attempts(client, db_session):
    _mock_send_text()
    client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    for _ in range(5):
        resp = client.post(
            "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": "000000"}
        )
        assert resp.status_code == 401

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": "000000"}
    )

    assert resp.status_code == 429


@respx.mock
def test_request_otp_sends_authentication_template_with_code(client, db_session):
    route = respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.otp"}]})
    )

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == settings.WHATSAPP_OTP_TEMPLATE_NAME

    user = db_session.query(User).filter(User.username == PHONE).first()
    components = payload["template"]["components"]
    assert components[0]["type"] == "body"
    assert components[0]["parameters"][0]["text"] == user.otp_code
    assert components[1]["type"] == "button"
    assert components[1]["sub_type"] == "copy_code"
    assert components[1]["parameters"][0]["coupon_code"] == user.otp_code


@respx.mock
def test_request_otp_falls_back_to_text_when_template_rejected(client, db_session):
    route = respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "template not approved"}}),
            httpx.Response(200, json={"messages": [{"id": "wamid.otp"}]}),
        ]
    )

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    assert route.call_count == 2
    first_payload = json.loads(route.calls[0].request.content)
    second_payload = json.loads(route.calls[1].request.content)
    assert first_payload["type"] == "template"
    assert second_payload["type"] == "text"


@respx.mock
def test_request_otp_skips_template_when_not_configured(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_OTP_TEMPLATE_NAME", None)
    route = _mock_send_text()

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["type"] == "text"


# --- /me -----------------------------------------------------------------------


def test_get_me_requires_auth(client):
    resp = client.get("/api/v1/auth/me")

    assert resp.status_code in (401, 403)


def test_get_me_returns_profile(client, auth_user):
    user, headers = auth_user

    resp = client.get("/api/v1/auth/me", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == user.id
    assert body["username"] == user.username
    assert body["phone_number"] is None


def test_patch_me_sets_phone_number(client, auth_user, db_session):
    user, headers = auth_user

    resp = client.patch("/api/v1/auth/me", json={"phone_number": "0712345999"}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["phone_number"] == "255712345999"
    db_session.refresh(user)
    assert user.phone_number == "255712345999"


def test_patch_me_rejects_phone_number_already_in_use(client, auth_user, db_session):
    _, headers = auth_user
    other = User(username="255700000050", phone_number="255711119999")
    db_session.add(other)
    db_session.commit()

    resp = client.patch("/api/v1/auth/me", json={"phone_number": "255711119999"}, headers=headers)

    assert resp.status_code == 400


def test_patch_me_allows_setting_same_phone_number_already_on_own_account(client, auth_user, db_session):
    user, headers = auth_user
    user.phone_number = "255711119999"
    db_session.commit()

    resp = client.patch("/api/v1/auth/me", json={"phone_number": "255711119999"}, headers=headers)

    assert resp.status_code == 200


def test_patch_me_without_phone_number_leaves_profile_unchanged(client, auth_user):
    user, headers = auth_user

    resp = client.patch("/api/v1/auth/me", json={}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["phone_number"] is None
