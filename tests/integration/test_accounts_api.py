import uuid

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import (
    NumberSubscription, NumberSubscriptionStatus, PhoneNumberPool, PhoneNumberStatus, User,
)

BASE = "/api/v1/accounts"
VALIDATE_URL_RE = r"https://graph\.facebook\.com/.+/PHONE1$"


@pytest.fixture
def other_user_headers(db_session):
    other = User(username="255700000040")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def _account_payload(**overrides):
    payload = {
        "name": "My Shop",
        "phone_number_id": "PHONE1",
        "access_token": "meta-token",
    }
    payload.update(overrides)
    return payload


@respx.mock
def test_create_account_validates_credentials_and_persists(client, auth_user):
    _, headers = auth_user
    route = respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 201
    assert route.called
    body = resp.json()
    assert body["phone_number_id"] == "PHONE1"
    assert "access_token" not in body


@respx.mock
def test_create_account_rejects_invalid_credentials(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(401))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 400


@respx.mock
def test_create_account_rejects_when_validation_request_errors(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(side_effect=httpx.ConnectError("boom"))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 400


def test_create_account_requires_auth(client):
    resp = client.post(BASE, json=_account_payload())

    assert resp.status_code in (401, 403)


@respx.mock
def test_list_accounts_scoped_to_current_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE2$").mock(
        return_value=httpx.Response(200)
    )

    client.post(BASE, json=_account_payload(name="Mine"), headers=headers)
    client.post(
        BASE,
        json=_account_payload(name="Theirs", phone_number_id="PHONE2"),
        headers=other_user_headers,
    )

    resp = client.get(BASE, headers=headers)

    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["Mine"]


@respx.mock
def test_update_account_without_credential_change_skips_validation(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"name": "Renamed"}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


@respx.mock
def test_update_account_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"name": "Hijacked"}, headers=other_user_headers
    )

    assert resp.status_code == 404


@respx.mock
def test_update_account_revalidates_when_token_changes(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    revalidate_route = respx.get(url__regex=VALIDATE_URL_RE).mock(
        return_value=httpx.Response(401)
    )

    resp = client.put(
        f"{BASE}/{created['id']}",
        json={"access_token": "new-bad-token"},
        headers=headers,
    )

    assert resp.status_code == 400
    assert revalidate_route.called


def test_create_account_partial_credentials_returns_400(client, auth_user):
    _, headers = auth_user

    resp = client.post(
        BASE,
        json={"name": "Half Creds Shop", "phone_number_id": "ONLY_PHONE"},
        headers=headers,
    )

    assert resp.status_code == 400


def test_create_account_both_credentials_omitted_returns_400(client, auth_user):
    # Numbers are connected manually for now (see #16) — no more silent
    # pool auto-assign when both fields are omitted.
    _, headers = auth_user

    resp = client.post(BASE, json={"name": "No Creds Shop"}, headers=headers)

    assert resp.status_code == 400


@respx.mock
def test_create_account_duplicate_phone_number_id_returns_clean_400(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    client.post(BASE, json=_account_payload(), headers=headers)

    resp = client.post(BASE, json=_account_payload(name="Second Shop"), headers=headers)

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


@respx.mock
def test_delete_account_releases_pool_number(client, auth_user, db_session):
    # An account can still end up linked to a PhoneNumberPool row via the
    # paid purchase flow or ops-driven manual assignment (see #16) — not
    # exercised here, just the resulting linked state delete must release.
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    db_session.add(
        PhoneNumberPool(
            phone_number_id="POOL_ACC_2",
            status=PhoneNumberStatus.ASSIGNED,
            whatsapp_account_id=uuid.UUID(created["id"]),
        )
    )
    db_session.commit()

    resp = client.delete(f"{BASE}/{created['id']}", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    pool_row = (
        db_session.query(PhoneNumberPool)
        .filter(PhoneNumberPool.phone_number_id == "POOL_ACC_2")
        .first()
    )
    assert pool_row.status.value == "RELEASED"
    assert pool_row.whatsapp_account_id is None


def test_delete_account_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=other_user_headers)

    assert resp.status_code == 404


def test_delete_account_requires_auth(client):
    resp = client.delete(f"{BASE}/1")

    assert resp.status_code in (401, 403)


def test_get_account_status_returns_meta_fields(client, auth_user):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    with respx.mock:
        respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "verified_name": "My Shop",
                    "display_phone_number": "+1 555-000-0000",
                    "quality_rating": "GREEN",
                    "code_verification_status": "VERIFIED",
                    "status": "CONNECTED",
                    "platform_type": "CLOUD_API",
                },
            )
        )
        resp = client.get(f"{BASE}/{created['id']}/status", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified_name"] == "My Shop"
    assert body["quality_rating"] == "GREEN"
    assert body["status"] == "CONNECTED"


def test_get_account_status_404_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.get(f"{BASE}/{created['id']}/status", headers=other_user_headers)

    assert resp.status_code == 404


def test_get_account_status_502_when_meta_errors(client, auth_user):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    with respx.mock:
        respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE1").mock(
            return_value=httpx.Response(400, json={"error": {}})
        )
        resp = client.get(f"{BASE}/{created['id']}/status", headers=headers)

    assert resp.status_code == 502


def test_get_account_status_requires_auth(client):
    resp = client.get(f"{BASE}/1/status")

    assert resp.status_code in (401, 403)


# --- purchase-number / number-subscription -----------------------------------

def _mock_initiate_payment(monkeypatch, ok=True):
    async def fake(self, phone, amount, reference):
        if not ok:
            raise RuntimeError("Tunzaa is down")
        return {"transaction_id": "T-fake", "status": "PENDING"}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake)


def test_purchase_number_success(client, auth_user, monkeypatch):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    _mock_initiate_payment(monkeypatch)

    resp = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "PENDING_PAYMENT"
    assert body["monthly_amount"] == 10000


def test_purchase_number_requires_auth(client):
    resp = client.post(f"{BASE}/1/purchase-number")

    assert resp.status_code in (401, 403)


def test_purchase_number_404_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.post(f"{BASE}/{created['id']}/purchase-number", headers=other_user_headers)

    assert resp.status_code == 404


def test_purchase_number_conflict_when_already_active(client, auth_user, db_session, monkeypatch):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    db_session.add(
        NumberSubscription(account_id=uuid.UUID(created["id"]), status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000)
    )
    db_session.commit()

    _mock_initiate_payment(monkeypatch)

    resp = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)

    assert resp.status_code == 409


def test_purchase_number_retries_by_reusing_deactivated_subscription(
    client, auth_user, db_session, monkeypatch
):
    # NumberSubscription.account_id is unique — a retry after a terminal
    # failure has to reuse the row, not insert a second one (which would
    # otherwise hit a raw DB IntegrityError instead of a clean 201).
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    existing = NumberSubscription(
        account_id=uuid.UUID(created["id"]),
        status=NumberSubscriptionStatus.DEACTIVATED,
        monthly_amount=10000,
        retry_count=1,
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    _mock_initiate_payment(monkeypatch)

    resp = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(existing.id)  # same row reused, not a new one
    assert body["status"] == "PENDING_PAYMENT"
    assert body["retry_count"] == 0
    assert db_session.query(NumberSubscription).filter(NumberSubscription.account_id == uuid.UUID(created["id"])).count() == 1


def test_purchase_number_payment_failure_returns_502(client, auth_user, monkeypatch):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    _mock_initiate_payment(monkeypatch, ok=False)

    resp = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)

    assert resp.status_code == 502


def test_purchase_number_failed_push_leaves_retryable_state_not_stuck(
    client, auth_user, db_session, monkeypatch
):
    # A push that fails outright (e.g. Tunzaa unreachable) must not leave
    # the subscription stuck in PENDING_PAYMENT — that's a blocking status
    # the endpoint itself 409s a retry against, permanently locking the
    # merchant out. Reproduces the actual failure seen live: Tunzaa's token
    # endpoint timed out, and the first PENDING_PAYMENT row blocked every
    # retry with 409 instead of allowing one.
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    _mock_initiate_payment(monkeypatch, ok=False)
    first = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)
    assert first.status_code == 502

    sub = db_session.query(NumberSubscription).filter(NumberSubscription.account_id == uuid.UUID(created["id"])).first()
    assert sub.status == NumberSubscriptionStatus.PAYMENT_FAILED

    _mock_initiate_payment(monkeypatch, ok=True)
    second = client.post(f"{BASE}/{created['id']}/purchase-number", headers=headers)

    assert second.status_code == 201
    assert second.json()["status"] == "PENDING_PAYMENT"


def test_get_number_subscription_returns_current_status(client, auth_user, db_session):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    db_session.add(
        NumberSubscription(
            account_id=uuid.UUID(created["id"]), status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000
        )
    )
    db_session.commit()

    resp = client.get(f"{BASE}/{created['id']}/number-subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"


def test_get_number_subscription_404_when_none(client, auth_user):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.get(f"{BASE}/{created['id']}/number-subscription", headers=headers)

    assert resp.status_code == 404


# --- subscription access gating -----------------------------------------------


def _seed_platform_product(db_session):
    """The gating check fails open when no Product is flagged as a plan —
    these tests are about a merchant with no *subscription*, not missing
    plans, so a flagged plan SKU must actually exist."""
    from app.db.models import Product

    platform_owner = User(username="255700000998")
    db_session.add(platform_owner)
    db_session.commit()
    db_session.refresh(platform_owner)

    product = Product(
        slug="taskflow-os-subscription", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(product)
    db_session.commit()


@respx.mock
def test_create_account_denies_ai_enabled_for_unsubscribed_merchant(client, auth_user, db_session, monkeypatch):
    _seed_platform_product(db_session)
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post(BASE, json=_account_payload(ai_enabled=True), headers=headers)

    assert resp.status_code == 201
    assert resp.json()["ai_enabled"] is False


@respx.mock
def test_update_account_denies_ai_enabled_for_unsubscribed_merchant(client, auth_user, db_session, monkeypatch):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    _seed_platform_product(db_session)

    resp = client.put(
        f"{BASE}/{created['id']}", json={"ai_enabled": True}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["ai_enabled"] is False


@respx.mock
def test_create_account_allows_ai_enabled_for_actively_subscribed_merchant(
    client, auth_user, db_session, monkeypatch
):
    from app.db.models import Customer, Order, OrderStatus, PaymentType, Product, ProductSubscription, ProductSubscriptionStatus

    user, headers = auth_user

    platform_owner = User(username="255700000999")
    db_session.add(platform_owner)
    db_session.commit()
    db_session.refresh(platform_owner)

    product = Product(
        slug="taskflow-os-subscription", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

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

    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post(BASE, json=_account_payload(ai_enabled=True), headers=headers)

    assert resp.status_code == 201
    assert resp.json()["ai_enabled"] is True
