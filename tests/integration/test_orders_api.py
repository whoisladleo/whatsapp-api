import uuid

import httpx
import pytest
import respx

from app.core.security import create_access_token
from app.db.models import (
    Customer,
    InstagramAccount,
    Message,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)

BASE = "/api/v1/orders"


def _mock_send_text():
    return respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.cancel"}]})
    )


@pytest.fixture
def merchant(db_session):
    user = User(username="255700000700")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def merchant_headers(merchant):
    return {"Authorization": f"Bearer {create_access_token(merchant.id)}"}


@pytest.fixture
def account(db_session, merchant):
    acc = WhatsAppAccount(
        name="Shop",
        phone_number_id="PHONE_ORD_1",
        access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)
    return acc


@pytest.fixture
def product(db_session, merchant):
    p = Product(
        slug="shoes",
        name="Shoes",
        max_price=50000,
        min_price=40000,
        quantity=5,
        merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711112222")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def order(db_session, account, product, customer):
    o = Order(
        customer_id=customer.id,
        product_id=product.id,
        account_id=account.id,
        quantity=1,
        agreed_price=45000,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def test_list_orders_requires_auth(client):
    resp = client.get(BASE)
    assert resp.status_code == 401


def test_list_orders_empty_for_merchant_with_no_orders(client, merchant_headers):
    resp = client.get(BASE, headers=merchant_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_orders_returns_merchant_order_with_derived_fields(
    client, merchant_headers, order, product, customer
):
    resp = client.get(BASE, headers=merchant_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(order.id)
    assert body[0]["product_name"] == product.name
    assert body[0]["customer_phone"] == customer.phone_number
    assert body[0]["status"] == "PENDING"


def test_list_orders_does_not_leak_other_merchants_orders(client, db_session, order):
    other = User(username="255700000701")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    resp = client.get(BASE, headers=other_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_orders_filters_by_status(client, merchant_headers, order, db_session):
    other_order = Order(
        customer_id=order.customer_id,
        product_id=order.product_id,
        account_id=order.account_id,
        quantity=1,
        agreed_price=42000,
        status=OrderStatus.PAID,
    )
    db_session.add(other_order)
    db_session.commit()

    resp = client.get(f"{BASE}?status=PAID", headers=merchant_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "PAID"


def test_list_orders_filters_by_account_id(client, merchant_headers, order, account):
    resp = client.get(f"{BASE}?account_id={account.id}", headers=merchant_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get(f"{BASE}?account_id={uuid.uuid4()}", headers=merchant_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_orders_includes_instagram_originated_order_for_instagram_only_merchant(
    client, db_session, product, customer
):
    """
    A merchant with no WhatsAppAccount at all (Instagram-only) must still
    see orders Amara closed over Instagram DM — this is exactly the gap
    Order.instagram_account_id + the outer-joined merchant scoping in
    get_orders_by_merchant fixes.
    """
    ig_merchant = User(username="255700000750")
    db_session.add(ig_merchant)
    db_session.commit()
    db_session.refresh(ig_merchant)
    ig_headers = {"Authorization": f"Bearer {create_access_token(ig_merchant.id)}"}

    ig_account = InstagramAccount(
        ig_business_id="ig_ord_1", access_token="ig-tok", user_id=ig_merchant.id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)

    ig_product = Product(
        slug="ig-order-shoes", name="IG Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=ig_merchant.id,
    )
    db_session.add(ig_product)
    db_session.commit()
    db_session.refresh(ig_product)

    ig_order = Order(
        customer_id=customer.id,
        product_id=ig_product.id,
        instagram_account_id=ig_account.id,
        quantity=1,
        agreed_price=45000,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
    )
    db_session.add(ig_order)
    db_session.commit()
    db_session.refresh(ig_order)

    resp = client.get(BASE, headers=ig_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(ig_order.id)
    assert body[0]["channel"] == "instagram"
    assert body[0]["account_id"] is None
    assert body[0]["instagram_account_id"] == str(ig_account.id)


def test_get_order_detail_includes_payment_progress(client, merchant_headers, db_session, order):
    order.paid_amount = 15000
    order.remaining_balance = 30000
    db_session.commit()

    resp = client.get(f"{BASE}/{order.id}", headers=merchant_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_amount"] == 15000
    assert body["remaining_balance"] == 30000
    # agreed_price=45000 on the `order` fixture -> 15000/45000 = 33.33%
    assert body["percentage_complete"] == pytest.approx(33.33)


def test_payment_history_requires_installment_plan(client, merchant_headers, order):
    resp = client.get(f"{BASE}/{order.id}/payment-history", headers=merchant_headers)
    assert resp.status_code == 400


def test_payment_history_not_found(client, merchant_headers):
    resp = client.get(f"{BASE}/{uuid.uuid4()}/payment-history", headers=merchant_headers)
    assert resp.status_code == 404


@respx.mock
def test_payment_history_returns_tunzaa_data(client, merchant_headers, db_session, order):
    order.tunzaa_plan_id = "1001"
    db_session.commit()

    respx.post("https://pay.tunzaa.co.tz/accounts/request/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    respx.get("https://pay.tunzaa.co.tz/installments/1001").mock(
        return_value=httpx.Response(
            200,
            json={
                "plan": {"plan_id": 1001, "remaining_balance": "30000.00"},
                "installments": [],
                "payment_history": [{"amount": "15000.00", "date": "2026-08-16"}],
            },
        )
    )

    resp = client.get(f"{BASE}/{order.id}/payment-history", headers=merchant_headers)

    assert resp.status_code == 200
    assert resp.json() == [{"amount": "15000.00", "date": "2026-08-16"}]


@respx.mock
def test_payment_history_returns_502_when_tunzaa_unreachable(
    client, merchant_headers, db_session, order
):
    order.tunzaa_plan_id = "1001"
    db_session.commit()

    respx.post("https://pay.tunzaa.co.tz/accounts/request/token").mock(
        side_effect=httpx.ConnectError("boom")
    )

    resp = client.get(f"{BASE}/{order.id}/payment-history", headers=merchant_headers)
    assert resp.status_code == 502


def test_get_order_detail_includes_delivery_address_and_installments(
    client, merchant_headers, db_session, order, customer
):
    customer.delivery_address = "123 Main St"
    db_session.commit()

    resp = client.get(f"{BASE}/{order.id}", headers=merchant_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_address"] == "123 Main St"
    assert body["installments"] == []


def test_get_order_not_found(client, merchant_headers):
    resp = client.get(f"{BASE}/{uuid.uuid4()}", headers=merchant_headers)
    assert resp.status_code == 404


def test_get_order_scoped_to_owning_merchant(client, db_session, order):
    other = User(username="255700000702")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}

    resp = client.get(f"{BASE}/{order.id}", headers=other_headers)
    assert resp.status_code == 404


@respx.mock
def test_cancel_pending_order_releases_stock_and_notifies_customer(
    client, merchant_headers, db_session, order, product
):
    route = _mock_send_text()
    starting_quantity = product.quantity

    resp = client.post(f"{BASE}/{order.id}/cancel", headers=merchant_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    db_session.refresh(product)
    assert product.quantity == starting_quantity + 1
    assert route.called

    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == order.customer.phone_number)
        .first()
    )
    assert outbound is not None
    assert outbound.status == "sent"


def test_cancel_already_paid_order_is_rejected(client, merchant_headers, db_session, order):
    order.status = OrderStatus.PAID
    db_session.commit()

    resp = client.post(f"{BASE}/{order.id}/cancel", headers=merchant_headers)

    assert resp.status_code == 400


def test_cancel_order_not_found(client, merchant_headers):
    resp = client.post(f"{BASE}/{uuid.uuid4()}/cancel", headers=merchant_headers)
    assert resp.status_code == 404


@respx.mock
def test_cancel_order_still_succeeds_when_whatsapp_send_raises(
    client, merchant_headers, order, db_session
):
    respx.post(url__regex=r"https://graph\.facebook\.com/.+/messages$").mock(
        side_effect=httpx.ConnectError("boom")
    )

    resp = client.post(f"{BASE}/{order.id}/cancel", headers=merchant_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"

    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == order.customer.phone_number)
        .first()
    )
    assert outbound is not None
    assert outbound.status == "failed"


def test_list_orders_includes_own_pay_now_order(client, merchant, merchant_headers, db_session):
    # Dashboard-native purchases carry no channel account — they show via
    # the recorded buying user, while another merchant's stay hidden (#16).
    other = User(username="255700000701")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    buyer = Customer(phone_number="255711113333")
    db_session.add(buyer)
    db_session.commit()
    db_session.refresh(buyer)
    plan = Product(
        slug="entry-plan", name="Taskflow Entry Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    mine = Order(
        customer_id=buyer.id, product_id=plan.id, buyer_user_id=merchant.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
    )
    theirs = Order(
        customer_id=buyer.id, product_id=plan.id, buyer_user_id=other.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
    )
    db_session.add_all([mine, theirs])
    db_session.commit()

    resp = client.get(BASE, headers=merchant_headers)

    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids
    assert [row for row in resp.json() if row["id"] == str(mine.id)][0]["channel"] == "dashboard"


def test_order_detail_visible_via_buyer_link(client, merchant, merchant_headers, db_session):
    buyer = Customer(phone_number="255711114444")
    db_session.add(buyer)
    db_session.commit()
    db_session.refresh(buyer)
    plan = Product(
        slug="entry-plan-2", name="Taskflow Entry Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    order = Order(
        customer_id=buyer.id, product_id=plan.id, buyer_user_id=merchant.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    resp = client.get(f"{BASE}/{order.id}", headers=merchant_headers)

    assert resp.status_code == 200
    assert resp.json()["id"] == str(order.id)


def test_channel_less_order_without_buyer_stays_hidden(client, merchant_headers, db_session):
    # Unmatched backfill rows (buyer_user_id NULL) keep today's behavior:
    # hidden, never leaked to a merchant view (#16).
    buyer = Customer(phone_number="255711115555")
    db_session.add(buyer)
    db_session.commit()
    db_session.refresh(buyer)
    plan = Product(
        slug="entry-plan-3", name="Taskflow Entry Plan", max_price=100000, min_price=100000,
        quantity=0, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    db_session.add(
        Order(
            customer_id=buyer.id, product_id=plan.id,
            quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
            status=OrderStatus.PAID,
        )
    )
    db_session.commit()

    resp = client.get(BASE, headers=merchant_headers)

    assert resp.status_code == 200
    assert resp.json() == []
