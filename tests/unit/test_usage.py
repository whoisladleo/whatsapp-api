import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

import app.tasks.usage_alerts as usage_alerts
from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    Customer,
    Order,
    OrderStatus,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)
from app.services import usage

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


@pytest.fixture
def merchant(db_session):
    user = User(username="255700000777")
    db_session.add(user)
    db_session.commit()
    db_session.add(WhatsAppAccount(
        name="Mama Neema Shoes", phone_number_id="USAGE_PNID", access_token="tok", user_id=user.id,
    ))
    db_session.commit()
    db_session.refresh(user)
    return user


def _platform_subscription(db_session, merchant, monkeypatch, status, next_billing_at):
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "taskflow-os-subscription")
    taskflow = User(username="255700000001")
    db_session.add(taskflow)
    db_session.commit()
    product = Product(
        slug="taskflow-os-subscription", name="Taskflow OS Subscription", max_price=100000,
        min_price=100000, quantity=999, merchant_id=taskflow.id, is_subscription=True,
    )
    customer = Customer(phone_number=merchant.username)
    db_session.add_all([product, customer])
    db_session.commit()
    order = Order(customer_id=customer.id, product_id=product.id, agreed_price=100000, status=OrderStatus.PAID)
    db_session.add(order)
    db_session.commit()
    db_session.add(ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=product.id, status=status,
        monthly_amount=100000, next_billing_at=next_billing_at,
    ))
    db_session.commit()


# --- billing_period ------------------------------------------------------------

def test_period_without_subscription_is_calendar_month_in_eat(db_session, merchant):
    # 22:00 UTC on Oct 31 is already Nov 1 in Dar es Salaam.
    start, end = usage.billing_period(db_session, merchant, now=datetime(2026, 10, 31, 22, 0))

    assert start == datetime(2026, 10, 31, 21, 0)  # Nov 1 00:00 EAT
    assert end == datetime(2026, 11, 30, 21, 0)  # Dec 1 00:00 EAT


def test_period_follows_active_platform_subscription(db_session, merchant, monkeypatch):
    next_billing = datetime(2026, 11, 5, 9, 0)
    _platform_subscription(db_session, merchant, monkeypatch, ProductSubscriptionStatus.ACTIVE, next_billing)

    start, end = usage.billing_period(db_session, merchant, now=datetime(2026, 10, 23))

    assert (start, end) == (next_billing - timedelta(days=30), next_billing)


def test_past_due_period_rolls_forward_past_a_stale_billing_date(db_session, merchant, monkeypatch):
    stale = datetime(2026, 10, 1)
    _platform_subscription(db_session, merchant, monkeypatch, ProductSubscriptionStatus.PAST_DUE, stale)

    start, end = usage.billing_period(db_session, merchant, now=datetime(2026, 10, 23))

    assert (start, end) == (stale, stale + timedelta(days=30))


def test_cancelled_subscription_falls_back_to_calendar_month(db_session, merchant, monkeypatch):
    _platform_subscription(
        db_session, merchant, monkeypatch, ProductSubscriptionStatus.CANCELLED, datetime(2026, 11, 5)
    )

    start, _ = usage.billing_period(db_session, merchant, now=datetime(2026, 10, 23))

    assert start == datetime(2026, 9, 30, 21, 0)


# --- record_reply / warnings ---------------------------------------------------

def test_warnings_fire_once_per_threshold(db_session, merchant, monkeypatch):
    monkeypatch.setattr(settings, "PLAN_REPLY_LIMIT", 5)
    dispatched = []
    monkeypatch.setattr(
        usage_alerts.send_usage_warning, "apply_async", lambda args, **kw: dispatched.append(tuple(args))
    )

    for _ in range(7):
        usage.record_reply(db_session, merchant.id, "whatsapp")

    assert dispatched == [(str(merchant.id), 80), (str(merchant.id), 100)]
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).count() == 7
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.USAGE_WARNING).count() == 2


def test_previous_period_warning_does_not_suppress_this_period(db_session, merchant, monkeypatch):
    monkeypatch.setattr(settings, "PLAN_REPLY_LIMIT", 1)
    dispatched = []
    monkeypatch.setattr(
        usage_alerts.send_usage_warning, "apply_async", lambda args, **kw: dispatched.append(args[1])
    )
    db_session.add(AmaraEvent(
        merchant_id=merchant.id, kind=AmaraEventKind.USAGE_WARNING, detail="80",
        created_at=datetime.utcnow() - timedelta(days=45),
    ))
    db_session.commit()

    usage.record_reply(db_session, merchant.id, "whatsapp")

    assert dispatched == [80, 100]


def test_record_event_never_raises(db_session, merchant, monkeypatch):
    def broken_commit():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "commit", broken_commit)

    usage.record_reply(db_session, merchant.id, "whatsapp")  # must not raise


def test_record_event_skips_missing_merchant(db_session):
    usage.record_event(db_session, None, AmaraEventKind.REPLY)

    assert db_session.query(AmaraEvent).count() == 0


# --- send_usage_warning ----------------------------------------------------------

@respx.mock
def test_usage_warning_goes_to_merchant_and_ops(db_session, merchant, monkeypatch):
    monkeypatch.setattr(usage_alerts, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", "255726000000")
    monkeypatch.setattr(settings, "PLAN_REPLY_LIMIT", 2000)
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.usage"}]})
    )

    usage_alerts.send_usage_warning(str(merchant.id), 80)

    sent = [json.loads(c.request.content) for c in route.calls]
    assert sorted(s["to"] for s in sent) == ["255700000777", "255726000000"]
    template = sent[0]["template"]
    assert template["name"] == "usage_warning"
    assert template["language"] == {"code": "en"}
    params = {p["parameter_name"]: p["text"] for p in template["components"][0]["parameters"]}
    assert params == {"account": "Mama Neema Shoes", "percent": "80%", "total": "2,000"}


@respx.mock
def test_usage_warning_without_ops_phone_only_alerts_merchant(db_session, merchant, monkeypatch):
    monkeypatch.setattr(usage_alerts, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "template in review"}})
    )

    result = usage_alerts.send_usage_warning(str(merchant.id), 100)

    assert route.call_count == 1  # a rejected send is logged, not raised
    assert result == "Sent to 1."
