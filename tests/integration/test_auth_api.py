import json
import uuid

import httpx
import jwt
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
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
def test_request_otp_rejects_blank_and_garbage_username(client, db_session):
    route = _mock_send_text()

    for bad in ("", "   ", "abc", "+--"):
        resp = client.post("/api/v1/auth/request-otp", json={"username": bad})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "Phone number is required"}

    assert db_session.query(User).count() == 0
    assert not route.called


@respx.mock
def test_request_otp_reuses_existing_user(client, db_session):
    _mock_send_text()
    existing = User(username=PHONE)
    db_session.add(existing)
    db_session.commit()

    client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert db_session.query(User).filter(User.username == PHONE).count() == 1


@respx.mock
def test_request_otp_promotes_phone_number_to_username_for_facebook_login_user(client, db_session):
    """
    A Facebook-login merchant (username=None) who's already self-reported
    their phone (PATCH /auth/me, Pay Now) must not get a second, unlinked
    User created just because they've never requested an OTP before — see
    issue #7. Requesting an OTP for that phone number should promote it to
    username on the existing account instead.
    """
    _mock_send_text()
    facebook_user = User(facebook_id="fb_123", phone_number=PHONE)
    db_session.add(facebook_user)
    db_session.commit()
    db_session.refresh(facebook_user)

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    assert db_session.query(User).count() == 1
    db_session.refresh(facebook_user)
    assert facebook_user.username == PHONE
    assert facebook_user.otp_code is not None


@respx.mock
def test_request_otp_does_not_promote_a_different_users_phone_number(client, db_session):
    """A phone_number match belonging to a user who already has their own
    username is a real, separate account — not a promotion case."""
    _mock_send_text()
    already_has_username = User(username="255799999999", phone_number=PHONE)
    db_session.add(already_has_username)
    db_session.commit()

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    assert db_session.query(User).filter(User.username == PHONE).count() == 1
    assert db_session.query(User).count() == 2


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
    assert uuid.UUID(payload["sub"]) == user.id

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
    # Contract change: unknown users get the SAME 401 shape as a bad code
    # (was 404 "User not found"), so responses don't reveal registration.
    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": "255700000000", "otp_code": "123456"}
    )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid OTP code"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


@respx.mock
def test_verify_otp_unknown_user_matches_bad_code_response(client, db_session):
    # Unknown-user and bad-code responses must be indistinguishable.
    _mock_send_text()
    client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    bad_code = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": "000000"}
    )
    unknown_user = client.post(
        "/api/v1/auth/verify-otp",
        json={"username": "255700000000", "otp_code": "000000"},
    )

    assert bad_code.status_code == unknown_user.status_code == 401
    assert bad_code.json() == unknown_user.json()


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
def test_request_otp_denied_when_redis_down(client, monkeypatch):
    # fail_open=False wiring: a Redis outage must 429 OTP sends, not
    # unlimit them.
    import redis

    import app.core.rate_limit as rate_limit_module

    class BrokenRedis:
        def incr(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 429


@respx.mock
def test_verify_otp_denied_when_redis_down(client, db_session, monkeypatch):
    import redis

    import app.core.rate_limit as rate_limit_module

    class BrokenRedis:
        def incr(self, key):
            raise redis.RedisError("connection refused")

    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())

    resp = client.post(
        "/api/v1/auth/verify-otp", json={"username": PHONE, "otp_code": "000000"}
    )

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
    # Must match the template's actual registered locale, not just its
    # display name ("English" is commonly "en", not "en_US") — a mismatch
    # fails with Meta error 132001.
    assert payload["template"]["language"]["code"] == settings.WHATSAPP_OTP_TEMPLATE_LANGUAGE

    user = db_session.query(User).filter(User.username == PHONE).first()
    components = payload["template"]["components"]
    assert components[0]["type"] == "body"
    assert components[0]["parameters"][0]["text"] == user.otp_code
    assert components[1]["type"] == "button"
    # taskflow_login's OTP button is registered as a "url"-type button
    # under the hood (Meta error 132018 confirmed this live — the Manager
    # UI's "Copy code" label doesn't always mean sub_type=copy_code), so
    # the code goes through as a plain text button parameter instead of
    # coupon_code.
    assert components[1]["sub_type"] == "url"
    assert components[1]["parameters"][0]["text"] == user.otp_code


@respx.mock
def test_request_otp_template_uses_configured_language_code(client, db_session, monkeypatch):
    """
    Regression test: Meta templates are keyed by (name, language) — a
    template Meta's UI displays as "English" is commonly registered under
    the bare "en" code, not "en_US". Sending the wrong one fails with
    error 132001 ("template name does not exist in <language>") even
    though the template exists and is approved. This must be configurable
    per-deployment rather than hardcoded, since it depends on how the
    template was actually created in WhatsApp Manager.
    """
    monkeypatch.setattr(settings, "WHATSAPP_OTP_TEMPLATE_LANGUAGE", "sw")
    route = respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.otp"}]})
    )

    resp = client.post("/api/v1/auth/request-otp", json={"username": PHONE})

    assert resp.status_code == 200
    payload = json.loads(route.calls[0].request.content)
    assert payload["template"]["language"]["code"] == "sw"


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
    assert body["id"] == str(user.id)
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


def test_patch_me_sets_handoff_number(client, auth_user, db_session):
    user, headers = auth_user

    resp = client.patch("/api/v1/auth/me", json={"handoff_phone_number": "0712345888"}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["handoff_phone_number"] == "255712345888"
    db_session.refresh(user)
    assert user.handoff_phone_number == "255712345888"


def test_patch_me_rejects_non_dialable_handoff_number(client, auth_user):
    _, headers = auth_user

    resp = client.patch("/api/v1/auth/me", json={"handoff_phone_number": "not-a-number"}, headers=headers)

    assert resp.status_code == 400


def test_patch_me_empty_handoff_number_clears_override(client, auth_user, db_session):
    user, headers = auth_user
    user.handoff_phone_number = "255712345888"
    db_session.commit()

    resp = client.patch("/api/v1/auth/me", json={"handoff_phone_number": ""}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["handoff_phone_number"] is None
    db_session.refresh(user)
    assert user.handoff_phone_number is None


# --- /me/subscription -----------------------------------------------------------

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
PLATFORM_SLUG = "taskflow-os-subscription"


def _seed_platform_product(db_session, slug=PLATFORM_SLUG, price=100000):
    from app.db.models import Product

    platform_owner = db_session.query(User).filter(User.username == "255700000997").first()
    if not platform_owner:
        platform_owner = User(username="255700000997")
        db_session.add(platform_owner)
        db_session.commit()
        db_session.refresh(platform_owner)

    product = Product(
        slug=slug, name="Taskflow OS Subscription", max_price=price, min_price=price,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_get_my_subscription_requires_auth(client):
    resp = client.get("/api/v1/auth/me/subscription")

    assert resp.status_code in (401, 403)


def test_get_my_subscription_none_when_no_subscription(client, auth_user, db_session, monkeypatch):
    _seed_platform_product(db_session)
    _, headers = auth_user

    resp = client.get("/api/v1/auth/me/subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "status": None, "monthly_amount": None, "next_billing_at": None,
        "retry_count": None, "past_due_since": None,
    }


def test_get_my_subscription_returns_status_when_found(client, auth_user, db_session, monkeypatch):
    from app.db.models import Customer, Order, OrderStatus, PaymentType, ProductSubscription, ProductSubscriptionStatus

    product = _seed_platform_product(db_session)
    user, headers = auth_user

    customer = Customer(phone_number=user.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1, agreed_price=100000,
        payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=product.id,
            status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        )
    )
    db_session.commit()

    resp = client.get("/api/v1/auth/me/subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"
    assert resp.json()["monthly_amount"] == 100000


# --- POST /me/subscription/pay --------------------------------------------------


def test_pay_now_requires_auth(client):
    resp = client.post("/api/v1/auth/me/subscription/pay", json={})

    assert resp.status_code in (401, 403)


def test_pay_now_503_when_not_configured(client, auth_user, monkeypatch):
    _, headers = auth_user

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 503


def test_pay_now_400_when_no_phone_on_file_and_none_supplied(client, db_session, monkeypatch):
    _seed_platform_product(db_session)

    fb_user = User(facebook_id="fb789")
    db_session.add(fb_user)
    db_session.commit()
    db_session.refresh(fb_user)
    headers = {"Authorization": f"Bearer {create_access_token(fb_user.id)}"}

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 400


@respx.mock
def test_pay_now_uses_existing_username_as_phone(client, auth_user, db_session, monkeypatch):
    _seed_platform_product(db_session)
    user, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_1"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert "order_id" in body
    assert payment_route.called
    sent = json.loads(payment_route.calls.last.request.content)
    assert sent["customer_msisdn"] == user.username


@respx.mock
def test_pay_now_claims_no_stock_for_subscriptions(client, auth_user, db_session, monkeypatch):
    # The seeded plan carries a bulk quantity, but plans have no units —
    # Pay Now must leave it untouched, like the chat close does (#119).
    product = _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_NOSTOCK"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    db_session.refresh(product)
    assert product.quantity == 999999


@respx.mock
def test_pay_now_with_inline_phone_number_saves_it_to_user(client, auth_user, db_session, monkeypatch):
    _seed_platform_product(db_session)

    fb_user = User(facebook_id="fb999")
    db_session.add(fb_user)
    db_session.commit()
    db_session.refresh(fb_user)
    headers = {"Authorization": f"Bearer {create_access_token(fb_user.id)}"}

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_2"})
    )

    resp = client.post(
        "/api/v1/auth/me/subscription/pay", json={"phone_number": "0712345888"}, headers=headers
    )

    assert resp.status_code == 200
    db_session.refresh(fb_user)
    assert fb_user.phone_number == "255712345888"
    sent = json.loads(payment_route.calls.last.request.content)
    assert sent["customer_msisdn"] == "255712345888"


def test_pay_now_400_when_inline_phone_already_used_by_another_user(client, auth_user, db_session, monkeypatch):
    _seed_platform_product(db_session)
    _, headers = auth_user

    other = User(username="255700000996", phone_number="255711119999")
    db_session.add(other)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/me/subscription/pay", json={"phone_number": "255711119999"}, headers=headers
    )

    assert resp.status_code == 400


@respx.mock
def test_pay_now_creates_order_and_pending_subscription(client, auth_user, db_session, monkeypatch):
    from app.db.models import Order, ProductSubscription, ProductSubscriptionStatus

    _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_3"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    order_id = resp.json()["order_id"]
    order = db_session.query(Order).filter(Order.id == uuid.UUID(order_id)).first()
    assert order is not None
    assert order.agreed_price == 100000
    assert order.tunzaa_transaction_id == "MOCK_TXN_PAYNOW_3"

    subscription = db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    assert subscription is not None
    assert subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


@respx.mock
def test_pay_now_called_twice_reuses_pending_order(client, auth_user, db_session, monkeypatch):
    """
    Regression test (#110): clicking Pay Now more than once before the
    first payment ever confirms (double-click, page refresh, abandoned
    Tunzaa prompt) must not mint a second Order — two orders means two
    real charges, and only one subscription row ever resolves. The second
    click reuses the pending order but still sends a fresh Tunzaa prompt,
    whose transaction id wins. (Was
    test_pay_now_called_twice_reuses_pending_subscription_row, which
    asserted the two-orders behavior — that expectation was the bug.)
    """
    from app.db.models import Order, ProductSubscription

    product = _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_FIRST"}),
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SECOND"}),
        ]
    )

    first = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    second = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]

    orders = db_session.query(Order).all()
    assert len(orders) == 1
    assert orders[0].tunzaa_transaction_id == "MOCK_TXN_SECOND"

    subscriptions = db_session.query(ProductSubscription).all()
    assert len(subscriptions) == 1
    assert subscriptions[0].order_id == orders[0].id

    # Plans have no units — Pay Now never takes stock for them (#119).
    db_session.refresh(product)
    assert product.quantity == 999999


@respx.mock
def test_pay_now_releases_reservation_on_dispatch_failure(client, auth_user, db_session, monkeypatch):
    from app.db.models import Order, OrderStatus

    product = _seed_platform_product(db_session)
    quantity_before = product.quantity
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 502
    db_session.refresh(product)
    assert product.quantity == quantity_before

    order = db_session.query(Order).filter(Order.product_id == product.id).first()
    assert order.status == OrderStatus.CANCELLED


@respx.mock
def test_pay_now_reused_order_dispatch_failure_restores_no_stock(client, auth_user, db_session, monkeypatch):
    # Plans never take stock at all (#119), so a dispatch failure must
    # cancel the order while leaving quantity untouched.
    from app.db.models import Order, OrderStatus

    product = _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_OK"}),
            httpx.Response(500),
        ]
    )

    first = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    assert first.status_code == 200
    db_session.refresh(product)
    assert product.quantity == 999999

    second = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    assert second.status_code == 502

    orders = db_session.query(Order).all()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.CANCELLED
    db_session.refresh(product)
    assert product.quantity == 999999


@respx.mock
def test_pay_now_does_not_reuse_pending_installment_order(client, auth_user, db_session, monkeypatch):
    # A pending INSTALLMENT order may carry a Tunzaa plan whose legs keep
    # billing — Pay Now (full payment) must mint its own order, never
    # repurpose that one (#110).
    from app.db.models import Customer, Order, OrderStatus, PaymentType

    product = _seed_platform_product(db_session)
    user, headers = auth_user

    customer = Customer(phone_number=user.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    installment_order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=100000, payment_type=PaymentType.INSTALLMENT,
        status=OrderStatus.PENDING, tunzaa_plan_id="plan-1",
    )
    db_session.add(installment_order)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_FULL"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["order_id"] != str(installment_order.id)
    fresh = db_session.query(Order).filter(Order.id == uuid.UUID(resp.json()["order_id"])).first()
    assert fresh.payment_type == PaymentType.FULL


@respx.mock
def test_pay_now_schedules_auto_cancel(client, auth_user, db_session, monkeypatch):
    # Dashboard orders never scheduled expiry (only chat closes did), so an
    # abandoned Pay Now prompt sat PENDING forever (#110).
    from app.tasks.order_reminders import auto_cancel_order

    _seed_platform_product(db_session)
    _, headers = auth_user

    scheduled = []
    monkeypatch.setattr(
        auto_cancel_order, "apply_async", lambda *a, **kw: scheduled.append((a, kw))
    )

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SCHED"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    assert len(scheduled) == 1
    assert scheduled[0][1]["args"] == [resp.json()["order_id"]]


# --- plan choice (#90) ----------------------------------------------------------


def test_pay_now_400_when_multiple_plans_and_no_choice(client, auth_user, db_session):
    _seed_platform_product(db_session)
    _seed_platform_product(db_session, slug="taskflow-os-pro", price=200000)
    _, headers = auth_user

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 400
    assert "plan" in resp.json()["detail"].lower()


def test_pay_now_400_for_unknown_plan_id(client, auth_user, db_session):
    _seed_platform_product(db_session)
    _, headers = auth_user

    resp = client.post(
        "/api/v1/auth/me/subscription/pay",
        json={"product_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert resp.status_code == 400


def test_pay_now_400_when_product_id_is_not_a_plan(client, auth_user, db_session):
    from app.db.models import Product

    _seed_platform_product(db_session)
    _, headers = auth_user
    user, _ = auth_user
    ordinary = Product(
        slug="shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=user.id,
    )
    db_session.add(ordinary)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/me/subscription/pay",
        json={"product_id": str(ordinary.id)},
        headers=headers,
    )

    assert resp.status_code == 400


@respx.mock
def test_pay_now_with_product_id_charges_that_plan(client, auth_user, db_session):
    from app.db.models import Order

    _seed_platform_product(db_session)
    pro = _seed_platform_product(db_session, slug="taskflow-os-pro", price=200000)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_PRO"})
    )

    resp = client.post(
        "/api/v1/auth/me/subscription/pay",
        json={"product_id": str(pro.id)},
        headers=headers,
    )

    assert resp.status_code == 200
    order = db_session.query(Order).filter(Order.id == uuid.UUID(resp.json()["order_id"])).one()
    assert order.product_id == pro.id
    assert order.agreed_price == 200000


@respx.mock
def test_pay_now_refuses_active_subscription(client, auth_user, db_session):
    # Paying for an ACTIVE plan is always a duplicate charge — 409 with no
    # new order and no Tunzaa call (#115). Tunzaa routes are deliberately
    # unmocked: any dispatch attempt fails this test.
    from app.db.models import (
        Customer,
        Order,
        OrderStatus,
        PaymentType,
        ProductSubscription,
        ProductSubscriptionStatus,
    )

    product = _seed_platform_product(db_session)
    user, headers = auth_user

    customer = Customer(phone_number=user.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=product.id,
            status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        )
    )
    db_session.commit()

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]
    assert db_session.query(Order).count() == 1


@respx.mock
def test_pay_now_allows_past_due_catch_up(client, auth_user, db_session):
    # PAST_DUE is a legitimate catch-up, not a duplicate (#115).
    from app.db.models import (
        Customer,
        Order,
        OrderStatus,
        PaymentType,
        ProductSubscription,
        ProductSubscriptionStatus,
    )

    product = _seed_platform_product(db_session)
    user, headers = auth_user

    customer = Customer(phone_number=user.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=product.id,
            status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000,
        )
    )
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PASTDUE"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    assert db_session.query(Order).count() == 2


@respx.mock
def test_pay_now_allows_resubscribe_after_cancelled(client, auth_user, db_session):
    # Resubscribing after CANCELLED is legitimate (#115).
    from app.db.models import (
        Customer,
        Order,
        OrderStatus,
        PaymentType,
        ProductSubscription,
        ProductSubscriptionStatus,
    )

    product = _seed_platform_product(db_session)
    user, headers = auth_user

    customer = Customer(phone_number=user.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=product.id,
            status=ProductSubscriptionStatus.CANCELLED, monthly_amount=100000,
        )
    )
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_RESUB"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    assert db_session.query(Order).count() == 2


# --- atomic Pay Now stock claim (P12) ------------------------------------------------


def _seed_platform_goods(db_session, slug="taskflow-os-goods", price=50000, quantity=1):
    # A platform plan that IS a physical good (units to claim) — the stock
    # path _seed_platform_product's subscription plans never exercise.
    from app.db.models import Product

    platform_owner = db_session.query(User).filter(User.username == "255700000997").first()
    if not platform_owner:
        platform_owner = User(username="255700000997")
        db_session.add(platform_owner)
        db_session.commit()
        db_session.refresh(platform_owner)

    product = Product(
        slug=slug, name="Taskflow OS Goods", max_price=price, min_price=price,
        quantity=quantity, merchant_id=platform_owner.id, is_subscription=False,
        is_platform_plan=True,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


@respx.mock
def test_pay_now_loses_last_unit_race_without_phantom_claim(client, auth_user, db_session):
    # P12: two customers racing the last unit used to check-then-decrement,
    # so both "claimed" it (quantity went to -1). The loser must now see 0
    # rows and take nothing — simulated deterministically (see
    # tests/unit/test_stock_claim.py): the row is moved to 0 behind the ORM
    # object's back, and the failed dispatch afterwards must restore nothing.
    from app.db.models import Customer, Order, OrderStatus, Product

    product = _seed_platform_goods(db_session)
    user, headers = auth_user
    # Pre-created so pay_now takes no commit (customer creation) before the
    # claim — any commit would expire the stale read below.
    db_session.add(Customer(phone_number=user.username))
    db_session.commit()

    db_session.query(Product).filter(Product.id == product.id).update(
        {Product.quantity: 0}, synchronize_session=False
    )
    assert product.quantity == 1  # stale, as a race loser sees it

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 502
    db_session.refresh(product)
    assert product.quantity == 0
    order = db_session.query(Order).filter(Order.product_id == product.id).first()
    assert order.status == OrderStatus.CANCELLED


@respx.mock
def test_pay_now_claims_last_unit_on_clean_path(client, auth_user, db_session):
    # Guardrail: uncontended, the atomic claim takes the unit exactly once.
    from app.db.models import Order, OrderStatus

    product = _seed_platform_goods(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_GOODS"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    db_session.refresh(product)
    assert product.quantity == 0
    order = db_session.query(Order).filter(Order.product_id == product.id).first()
    assert order.status == OrderStatus.PENDING
    assert order.tunzaa_transaction_id == "MOCK_TXN_GOODS"


@respx.mock
def test_pay_now_reused_order_claims_no_second_unit(client, auth_user, db_session):
    # Guardrail: a second click reuses the pending order and claims nothing more.
    from app.db.models import Order

    product = _seed_platform_goods(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_G1"}),
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_G2"}),
        ]
    )

    first = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    second = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] == second.json()["order_id"]
    assert db_session.query(Order).count() == 1
    db_session.refresh(product)
    assert product.quantity == 0


@respx.mock
def test_pay_now_dispatch_failure_after_won_claim_releases_unit(client, auth_user, db_session):
    # P12 symmetry: a claimed unit on a failed dispatch goes back — the
    # failure path must not leak stock any more than it mints phantoms.
    from app.db.models import Order, OrderStatus

    product = _seed_platform_goods(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 502
    db_session.refresh(product)
    assert product.quantity == 1
    order = db_session.query(Order).filter(Order.product_id == product.id).first()
    assert order.status == OrderStatus.CANCELLED
