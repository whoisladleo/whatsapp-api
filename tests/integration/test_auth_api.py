import json

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


# --- /me/subscription -----------------------------------------------------------

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
PLATFORM_SLUG = "taskflow-os-subscription"


def _seed_platform_product(db_session, slug=PLATFORM_SLUG, price=100000):
    from app.db.models import Product

    platform_owner = User(username="255700000997")
    db_session.add(platform_owner)
    db_session.commit()
    db_session.refresh(platform_owner)

    product = Product(
        slug=slug, name="Taskflow OS Subscription", max_price=price, min_price=price,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_get_my_subscription_requires_auth(client):
    resp = client.get("/api/v1/auth/me/subscription")

    assert resp.status_code in (401, 403)


def test_get_my_subscription_none_when_no_subscription(client, auth_user, db_session, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", None)
    _, headers = auth_user

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 503


def test_pay_now_400_when_no_phone_on_file_and_none_supplied(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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
def test_pay_now_with_inline_phone_number_saves_it_to_user(client, auth_user, db_session, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
    _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_3"})
    )

    resp = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert resp.status_code == 200
    order_id = resp.json()["order_id"]
    order = db_session.query(Order).filter(Order.id == order_id).first()
    assert order is not None
    assert order.agreed_price == 100000
    assert order.tunzaa_transaction_id == "MOCK_TXN_PAYNOW_3"

    subscription = db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    assert subscription is not None
    assert subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


@respx.mock
def test_pay_now_called_twice_reuses_pending_subscription_row(client, auth_user, db_session, monkeypatch):
    """
    Regression test: clicking Pay Now more than once before the first
    payment ever confirms (abandoned Tunzaa prompt, page refresh, etc.)
    must not accumulate one orphaned PENDING_PAYMENT ProductSubscription
    row per click — see product_subscription.get_or_create_pending_subscription.
    """
    from app.db.models import Order, ProductSubscription

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
    _seed_platform_product(db_session)
    _, headers = auth_user

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PAYNOW_DUP"})
    )

    first = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    second = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["order_id"] != second.json()["order_id"]

    orders = db_session.query(Order).filter(Order.id.in_([first.json()["order_id"], second.json()["order_id"]])).all()
    assert len(orders) == 2

    subscriptions = db_session.query(ProductSubscription).all()
    assert len(subscriptions) == 1
    assert subscriptions[0].order_id == second.json()["order_id"]


@respx.mock
def test_pay_now_releases_reservation_on_dispatch_failure(client, auth_user, db_session, monkeypatch):
    from app.db.models import Order, OrderStatus

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PLATFORM_SLUG)
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
