from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    Customer,
    InstagramAccount,
    InstagramCustomer,
    InstagramDirectMessage,
    Message,
    Order,
    OrderStatus,
    Product,
    User,
    WhatsAppAccount,
)
from app.services import usage

URL = "/api/v1/stats"


@pytest.fixture
def shop(db_session, auth_user):
    user, _ = auth_user
    wa = WhatsAppAccount(name="Stats Shop", phone_number_id="STATS_PNID", access_token="tok", user_id=user.id)
    ig = InstagramAccount(ig_business_id="ig_stats", username="statsshop", access_token="ig-tok", user_id=user.id)
    shoes = Product(slug="stats-shoes", name="Shoes", max_price=50000, min_price=40000, quantity=5, merchant_id=user.id)
    bag = Product(slug="stats-bag", name="Bag", max_price=30000, min_price=20000, quantity=5, merchant_id=user.id)
    db_session.add_all([wa, ig, shoes, bag])
    db_session.commit()
    return user, wa, ig, shoes, bag


def _event(db_session, merchant, kind, created_at=None, **kw):
    db_session.add(AmaraEvent(merchant_id=merchant.id, kind=kind, created_at=created_at or datetime.utcnow(), **kw))


def test_stats_requires_auth(client):
    assert client.get(URL).status_code == 401


def test_current_period_stats(client, db_session, auth_user, shop, monkeypatch):
    monkeypatch.setattr(settings, "PLAN_REPLY_LIMIT", 10)
    user, wa, ig, shoes, bag = shop
    _, headers = auth_user
    start, _ = usage.billing_period(db_session, user)

    # Buyers: two WhatsApp numbers (one twice, one written with a 0 prefix),
    # the merchant's own command (excluded), and one Instagram DM customer.
    for phone in ("255711000001", "255711000001", "0711000002", user.username):
        db_session.add(Message(from_phone=phone, type="text", body="hi", direction="inbound", account_id=wa.id))
    ig_customer = InstagramCustomer(ig_scoped_id="igsid_stats", account_id=ig.id)
    db_session.add(ig_customer)
    db_session.commit()
    db_session.add(InstagramDirectMessage(
        account_id=ig.id, customer_id=ig_customer.id, direction="inbound", body="bei?",
    ))

    buyer = Customer(phone_number="255711000001")
    db_session.add(buyer)
    db_session.commit()
    db_session.add_all([
        Order(customer_id=buyer.id, account_id=wa.id, product_id=shoes.id, agreed_price=45000, status=OrderStatus.PAID),
        Order(customer_id=buyer.id, account_id=wa.id, product_id=bag.id, agreed_price=25000, status=OrderStatus.PENDING),
    ])

    for _ in range(3):
        _event(db_session, user, AmaraEventKind.REPLY, channel="whatsapp")
    _event(db_session, user, AmaraEventKind.REPLY, created_at=start - timedelta(days=1))  # last period
    _event(db_session, user, AmaraEventKind.QUOTE, product_name="Shoes", amount=45000)
    _event(db_session, user, AmaraEventKind.QUOTE, product_name="Shoes", amount=47000)
    _event(db_session, user, AmaraEventKind.QUOTE, product_name="Bag", amount=25000)
    _event(db_session, user, AmaraEventKind.HANDOFF, channel="whatsapp")
    _event(db_session, user, AmaraEventKind.PAYMENT, amount=45000)

    # Another merchant's activity must never show up.
    other = User(username="255700099999")
    db_session.add(other)
    db_session.commit()
    _event(db_session, other, AmaraEventKind.REPLY)
    _event(db_session, other, AmaraEventKind.PAYMENT, amount=999999)
    db_session.commit()

    body = client.get(URL, headers=headers).json()

    assert body["replies"] == {"used": 3, "limit": 10, "percent_used": 30.0}
    assert body["buyers"] == 3
    assert body["handoffs"] == 1
    assert body["orders_created"] == 2
    assert body["orders_paid"] == 1
    assert body["tzs_collected"] == 45000
    assert body["conversion_rate"] == round(1 / 3, 3)
    assert body["top_products"] == [
        {"product": "Shoes", "quotes": 2, "orders": 1, "paid_orders": 1},
        {"product": "Bag", "quotes": 1, "orders": 1, "paid_orders": 0},
    ]


def test_dashboard_limit_uses_plan_not_global_default(client, db_session, auth_user, shop, monkeypatch):
    from app.db.models import ProductSubscription, ProductSubscriptionStatus

    monkeypatch.setattr(settings, "PLAN_REPLY_LIMIT", 10)
    user, *_ = shop
    _, headers = auth_user
    seller = User(username="255700099998")
    buyer = Customer(phone_number=user.username)
    db_session.add_all([seller, buyer])
    db_session.commit()
    plan = Product(
        slug="stats-plan", name="Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=seller.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=50,
    )
    db_session.add(plan)
    db_session.commit()
    order = Order(customer_id=buyer.id, product_id=plan.id, agreed_price=100000, status=OrderStatus.PAID)
    db_session.add(order)
    db_session.commit()
    db_session.add(ProductSubscription(
        order_id=order.id, customer_id=buyer.id, product_id=plan.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        next_billing_at=datetime.utcnow() + timedelta(days=5),
    ))
    db_session.commit()

    body = client.get(URL, headers=headers).json()

    assert body["replies"]["limit"] == 50


def test_previous_period(client, db_session, auth_user, shop):
    user, *_ = shop
    _, headers = auth_user
    start, _ = usage.billing_period(db_session, user)
    _event(db_session, user, AmaraEventKind.REPLY, created_at=start - timedelta(days=1))
    _event(db_session, user, AmaraEventKind.REPLY)
    db_session.commit()

    body = client.get(URL, params={"period": "previous"}, headers=headers).json()

    assert body["replies"]["used"] == 1
    assert body["period_end"].startswith(start.isoformat()[:16])


def test_empty_period(client, auth_user, shop):
    _, headers = auth_user

    body = client.get(URL, headers=headers).json()

    assert body["replies"]["used"] == 0
    assert body["buyers"] == 0
    assert body["conversion_rate"] is None
    assert body["top_products"] == []
