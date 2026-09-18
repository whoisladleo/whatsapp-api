import uuid

import httpx
import jwt
import pytest
import respx

from app.core.config import settings
from app.db.models import InstagramAccount, User

SHORT_TOKEN_URL_RE = r"https://api\.instagram\.com/oauth/access_token$"
LONG_TOKEN_URL_RE = r"https://graph\.instagram\.com/access_token"
ME_URL_RE = r"https://graph\.instagram\.com/me\?"
SUBSCRIBE_URL_RE = r"https://graph\.instagram\.com/me/subscribed_apps"


@pytest.fixture(autouse=True)
def _ig_config(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "APPID123")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "APPSECRET123")
    monkeypatch.setattr(settings, "INSTAGRAM_REDIRECT_URI", "https://api.example.com/cb")
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://dash.example.com")


def _mock_successful_exchange(ig_user_id="ig_user_1", username="shopowner"):
    respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(
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
    return respx.post(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )


def test_start_oauth_requires_auth(client):
    resp = client.get("/api/v1/instagram/oauth/start")

    assert resp.status_code in (401, 403)


def test_start_oauth_returns_authorize_url_with_signed_state(client, auth_user):
    user, headers = auth_user

    resp = client.get("/api/v1/instagram/oauth/start", headers=headers)

    assert resp.status_code == 200
    authorize_url = resp.json()["authorize_url"]
    assert authorize_url.startswith(settings.INSTAGRAM_OAUTH_AUTHORIZE_URL)
    assert "client_id=APPID123" in authorize_url

    state = authorize_url.split("state=")[1].split("&")[0]
    payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert uuid.UUID(payload["sub"]) == user.id


def test_callback_missing_code_redirects_to_error(client):
    resp = client.get(
        "/api/v1/instagram/oauth/callback", params={"state": "x"}, follow_redirects=False
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/dashboard/settings?instagram=error"


def test_callback_invalid_state_redirects_to_error(client):
    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": "not-a-real-token"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/dashboard/settings?instagram=error"


def _valid_state_for(user_id: int) -> str:
    from app.core.security import create_access_token

    return create_access_token(user_id)


@respx.mock
def test_callback_success_creates_instagram_account(client, db_session, auth_user):
    user, _ = auth_user
    subscribe_route = _mock_successful_exchange(ig_user_id="ig_user_1", username="shopowner")

    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/dashboard/settings?instagram=connected"

    account = db_session.query(InstagramAccount).filter(
        InstagramAccount.ig_business_id == "ig_user_1"
    ).first()
    assert account is not None
    assert account.access_token == "long-tok"
    assert account.username == "shopowner"
    assert account.user_id == user.id

    assert subscribe_route.called
    assert subscribe_route.calls.last.request.url.params["access_token"] == "long-tok"


@respx.mock
def test_callback_still_connects_account_even_if_webhook_subscription_fails(
    client, db_session, auth_user
):
    user, _ = auth_user
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
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/dashboard/settings?instagram=connected"
    account = db_session.query(InstagramAccount).filter(
        InstagramAccount.ig_business_id == "ig_user_1"
    ).first()
    assert account is not None


@respx.mock
def test_callback_reconnect_updates_existing_account_instead_of_duplicating(
    client, db_session, auth_user
):
    user, _ = auth_user
    existing = InstagramAccount(
        ig_business_id="ig_user_1", access_token="old-tok", username="old_name", user_id=user.id
    )
    db_session.add(existing)
    db_session.commit()

    _mock_successful_exchange(ig_user_id="ig_user_1", username="new_name")

    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert db_session.query(InstagramAccount).count() == 1
    db_session.refresh(existing)
    assert existing.access_token == "long-tok"
    assert existing.username == "new_name"


def _seed_platform_product(db_session, slug="taskflow-os-subscription"):
    from app.db.models import Product

    platform_owner = User(username="255700000997")
    db_session.add(platform_owner)
    db_session.commit()
    db_session.refresh(platform_owner)

    product = Product(
        slug=slug, name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
    )
    db_session.add(product)
    db_session.commit()
    return product


@respx.mock
def test_callback_denies_ai_enabled_for_unsubscribed_merchant(client, db_session, auth_user, monkeypatch):
    user, _ = auth_user
    _seed_platform_product(db_session)
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "taskflow-os-subscription")
    _mock_successful_exchange(ig_user_id="ig_user_1", username="shopowner")

    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    account = db_session.query(InstagramAccount).filter(
        InstagramAccount.ig_business_id == "ig_user_1"
    ).first()
    assert account is not None
    assert account.ai_enabled is False


@respx.mock
def test_callback_allows_ai_enabled_for_actively_subscribed_merchant(client, db_session, auth_user, monkeypatch):
    from app.db.models import Customer, Order, OrderStatus, PaymentType, ProductSubscription, ProductSubscriptionStatus

    user, _ = auth_user
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "taskflow-os-subscription")
    product = _seed_platform_product(db_session)

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

    _mock_successful_exchange(ig_user_id="ig_user_1", username="shopowner")

    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    account = db_session.query(InstagramAccount).filter(
        InstagramAccount.ig_business_id == "ig_user_1"
    ).first()
    assert account is not None
    assert account.ai_enabled is True


@respx.mock
def test_callback_token_exchange_failure_redirects_to_error(client, db_session, auth_user):
    user, _ = auth_user
    respx.post(url__regex=SHORT_TOKEN_URL_RE).mock(return_value=httpx.Response(400, json={}))

    resp = client.get(
        "/api/v1/instagram/oauth/callback",
        params={"code": "auth-code", "state": _valid_state_for(user.id)},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://dash.example.com/dashboard/settings?instagram=error"
    assert db_session.query(InstagramAccount).count() == 0
