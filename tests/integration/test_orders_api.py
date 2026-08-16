import httpx
import pytest
import respx

from app.core.security import create_access_token
from app.db.models import (
    Customer,
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
    assert body[0]["id"] == order.id
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

    resp = client.get(f"{BASE}?account_id=999999", headers=merchant_headers)
    assert resp.status_code == 200
    assert resp.json() == []


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
    resp = client.get(f"{BASE}/999999", headers=merchant_headers)
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
    resp = client.post(f"{BASE}/999999/cancel", headers=merchant_headers)
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
