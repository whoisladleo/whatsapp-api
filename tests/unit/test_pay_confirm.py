"""Reply-YES subscription payment (chat twin to dashboard Pay Now)."""

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.db.models import (
    Customer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
)
from app.services import self_service_billing

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"


def _mock_payments(txn_ids=("MOCK_TXN_YES",)):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    return respx.post(PAYMENT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": txn})
            for txn in txn_ids
        ]
    )


@pytest.fixture
def platform_owner(db_session):
    u = User(username="255700009000")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def platform_product(db_session, platform_owner):
    p = Product(
        slug="taskflow-os-subscription", name="Taskflow OS Subscription",
        max_price=100000, min_price=100000, quantity=999999,
        merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def merchant(db_session):
    u = User(username="255711220000")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _pending_orders(db_session, customer, product):
    return (
        db_session.query(Order)
        .filter(
            Order.customer_id == customer.id,
            Order.product_id == product.id,
            Order.status == OrderStatus.PENDING,
            Order.payment_type == PaymentType.FULL,
        )
        .all()
    )


@respx.mock
async def test_yes_creates_pending_order_and_dispatches_push(
    db_session, platform_product, merchant
):
    pay_route = _mock_payments()

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "YES"
    )

    assert pay_route.call_count == 1
    assert "sent to 255711220000" in reply
    customer = db_session.query(Customer).filter_by(phone_number=merchant.username).one()
    orders = _pending_orders(db_session, customer, platform_product)
    assert len(orders) == 1
    assert orders[0].agreed_price == 100000


@respx.mock
async def test_yes_is_case_insensitive_and_strips(db_session, platform_product, merchant):
    pay_route = _mock_payments()

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "  NdiyO  "
    )

    assert pay_route.call_count == 1
    assert "sent to" in reply


@respx.mock
async def test_yes_inside_throttle_window_sends_nothing(
    db_session, platform_product, merchant
):
    pay_route = _mock_payments(txn_ids=("TXN_ONE", "TXN_TWO"))
    await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    assert pay_route.call_count == 1
    assert "already on its way" in reply


@respx.mock
async def test_yes_after_window_reuses_same_order(db_session, platform_product, merchant):
    pay_route = _mock_payments(txn_ids=("TXN_ONE", "TXN_TWO"))
    await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )
    customer = db_session.query(Customer).filter_by(phone_number=merchant.username).one()
    order = _pending_orders(db_session, customer, platform_product)[0]
    order.updated_at = datetime.utcnow() - timedelta(minutes=6)
    db_session.commit()

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    assert pay_route.call_count == 2
    assert "sent to" in reply
    assert len(_pending_orders(db_session, customer, platform_product)) == 1


@respx.mock
async def test_non_yes_falls_through(db_session, platform_product, merchant):
    pay_route = _mock_payments()

    assert (
        await self_service_billing.confirm_subscription_payment(
            db_session, merchant, merchant.username, "hello there"
        )
    ) is None
    assert (
        await self_service_billing.confirm_subscription_payment(
            db_session, merchant, merchant.username, "yesterday"
        )
    ) is None
    assert pay_route.call_count == 0


@respx.mock
async def test_yes_with_no_plans_falls_through(db_session, merchant):
    pay_route = _mock_payments()

    assert (
        await self_service_billing.confirm_subscription_payment(
            db_session, merchant, merchant.username, "yes"
        )
    ) is None
    assert pay_route.call_count == 0


@respx.mock
async def test_yes_when_already_subscribed_sends_nothing(
    db_session, platform_product, merchant
):
    pay_route = _mock_payments()
    customer = Customer(phone_number=merchant.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=platform_product.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=platform_product.id,
            monthly_amount=100000, status=ProductSubscriptionStatus.ACTIVE,
        )
    )
    db_session.commit()

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    assert pay_route.call_count == 0
    assert "already active" in reply


@respx.mock
async def test_yes_with_multiple_plans_points_to_dashboard(
    db_session, platform_product, platform_owner, merchant, monkeypatch
):
    monkeypatch.setattr(
        "app.services.self_service_billing.settings.DASHBOARD_URL",
        "https://dash.example.test/",
    )
    db_session.add(
        Product(
            slug="taskflow-os-pro", name="Taskflow OS Pro",
            max_price=200000, min_price=200000, quantity=999999,
            merchant_id=platform_owner.id, is_subscription=True,
            is_platform_plan=True, monthly_reply_limit=5000,
        )
    )
    db_session.commit()
    pay_route = _mock_payments()

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    assert pay_route.call_count == 0
    assert "https://dash.example.test/dashboard/settings" in reply


@respx.mock
async def test_yes_when_tunzaa_down_reports_network_error(
    db_session, platform_product, merchant
):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    reply = await self_service_billing.confirm_subscription_payment(
        db_session, merchant, merchant.username, "yes"
    )

    assert "couldn't reach the payment network" in reply
