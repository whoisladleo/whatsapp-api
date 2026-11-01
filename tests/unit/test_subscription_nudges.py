"""Subscription-nudge sweep + daily dispatch trigger + amara_active flag."""

from datetime import datetime, timedelta

import pytest

import app.core.rate_limit as rate_limit_module
from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    Customer,
    InstagramAccount,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)
from app.tasks import subscription_nudges as nudge_module


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


@pytest.fixture
def merchant_channel(db_session, merchant):
    a = WhatsAppAccount(
        name="shop", phone_number_id="pn_merchant", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def _task_uses_test_session(monkeypatch, db_session):
    monkeypatch.setattr(nudge_module, "SessionLocal", lambda: db_session)


@pytest.fixture
def _platform_creds(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "test-platform-token")
    monkeypatch.setattr(settings, "PHONE_NUMBER_ID", "test-phone-id")


class _FakeProvider:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def send_template(self, to, name, language_code=None, components=None):
        _FakeProvider.calls.append(
            {"to": to, "name": name, "language_code": language_code, "components": components}
        )
        return {"messages": [{"id": "wamid_nudge"}]}


class _FailingProvider(_FakeProvider):
    async def send_template(self, to, name, language_code=None, components=None):
        _FakeProvider.calls.append({"to": to})
        return {"error": {"message": "template not approved"}}


@pytest.fixture
def _fake_sends(monkeypatch):
    _FakeProvider.calls = []
    monkeypatch.setattr(nudge_module, "WhatsAppProvider", _FakeProvider)
    return _FakeProvider.calls


def _make_active_sub(db_session, platform_product, merchant):
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
    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=platform_product.id,
        monthly_amount=100000, status=ProductSubscriptionStatus.ACTIVE,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _record_nudges(db_session, merchant, count, days_ago_last):
    base = datetime.utcnow() - timedelta(days=days_ago_last)
    for i in range(count):
        db_session.add(
            AmaraEvent(
                merchant_id=merchant.id,
                kind=AmaraEventKind.SUBSCRIPTION_NUDGE,
                channel="whatsapp",
                created_at=base - timedelta(days=30 * (count - 1 - i)),
            )
        )
    db_session.commit()


def _nudge_events(db_session, merchant_id):
    # Takes the id, not the row: the sweep closes the session when done,
    # which detaches every fixture instance it committed over.
    return (
        db_session.query(AmaraEvent)
        .filter(
            AmaraEvent.merchant_id == merchant_id,
            AmaraEvent.kind == AmaraEventKind.SUBSCRIPTION_NUDGE,
        )
        .all()
    )


# --- targeting ---------------------------------------------------------------


def test_sweep_sends_to_connected_unpaid_merchant(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    phone = merchant.username
    merchant_id = merchant.id
    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 1 subscription nudges" in result
    assert [c["to"] for c in _fake_sends] == [phone]
    assert _fake_sends[0]["name"] == settings.WHATSAPP_NUDGE_TEMPLATE_NAME
    # The approved template has no variables — parameters would be rejected.
    assert _fake_sends[0]["components"] is None
    assert len(_nudge_events(db_session, merchant_id)) == 1


def test_sweep_skips_paid_merchant(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    _make_active_sub(db_session, platform_product, merchant)

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _fake_sends == []


def test_sweep_skips_merchant_with_no_connected_channel(
    db_session, platform_product, merchant,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _fake_sends == []


def test_sweep_counts_connected_instagram_as_a_channel(
    db_session, platform_product, merchant,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    db_session.add(
        InstagramAccount(
            ig_business_id="ig_1", access_token="tok", username="shop",
            user_id=merchant.id,
        )
    )
    db_session.commit()

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 1 subscription nudges" in result


def test_sweep_ignores_disconnected_instagram_only(
    db_session, platform_product, merchant,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    db_session.add(
        InstagramAccount(
            ig_business_id="ig_1", access_token=None, username="shop",
            user_id=merchant.id,
        )
    )
    db_session.commit()

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result


def test_sweep_skips_merchant_without_phone(
    db_session, platform_product, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    merchant_channel.owner.username = None
    merchant_channel.owner.phone_number = None
    db_session.commit()

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _fake_sends == []


def test_sweep_skips_plan_seller(
    db_session, platform_product, platform_owner,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    db_session.add(
        WhatsAppAccount(
            name="taskflow", phone_number_id="pn_platform", access_token="tok",
            user_id=platform_owner.id,
        )
    )
    db_session.commit()

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _fake_sends == []


def test_sweep_sends_nothing_when_no_plans_configured(
    db_session, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    result = nudge_module.send_subscription_nudges.run()

    assert "No platform plans" in result
    assert _fake_sends == []


def test_sweep_skips_sends_when_template_unset(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends, monkeypatch,
):
    monkeypatch.setattr(settings, "WHATSAPP_NUDGE_TEMPLATE_NAME", None)
    merchant_id = merchant.id

    result = nudge_module.send_subscription_nudges.run()

    assert "not configured" in result
    assert _fake_sends == []
    assert _nudge_events(db_session, merchant_id) == []


def test_sweep_skips_sends_when_platform_creds_missing(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _fake_sends, monkeypatch,
):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", None)

    result = nudge_module.send_subscription_nudges.run()

    assert "credentials missing" in result
    assert _fake_sends == []


def test_failed_send_records_no_event(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, monkeypatch,
):
    _FakeProvider.calls = []
    monkeypatch.setattr(nudge_module, "WhatsAppProvider", _FailingProvider)
    merchant_id = merchant.id

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _nudge_events(db_session, merchant_id) == []


# --- schedule ------------------------------------------------------------------


def test_first_nudge_always_due(db_session, merchant):
    assert nudge_module._nudge_due([], datetime.utcnow()) is True


@pytest.mark.parametrize(
    "count,days_ago_last,expected",
    [
        (1, 1, False),  # nudge 2 needs a 2-day gap
        (1, 3, True),
        (2, 3, False),  # nudge 3 needs a 5-day gap (day 7)
        (2, 6, True),
        (5, 6, False),  # nudge 4+ are weekly
        (5, 8, True),
        (29, 30, True),  # 30th nudge still allowed
        (30, 365, False),  # cap reached: silence, however long ago
    ],
)
def test_nudge_schedule_gaps(db_session, merchant, count, days_ago_last, expected):
    _record_nudges(db_session, merchant, count, days_ago_last)
    history = nudge_module._nudge_history(db_session, merchant.id)

    assert len(history) == count
    assert nudge_module._nudge_due(history, datetime.utcnow()) is expected


def test_sweep_respects_cap(
    db_session, platform_product, merchant, merchant_channel,
    _task_uses_test_session, _platform_creds, _fake_sends,
):
    _record_nudges(db_session, merchant, 30, 400)

    result = nudge_module.send_subscription_nudges.run()

    assert "Sent 0 subscription nudges" in result
    assert _fake_sends == []


# --- daily dispatch trigger ----------------------------------------------------


def test_trigger_dispatches_once_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(
        nudge_module.send_subscription_nudges,
        "apply_async",
        lambda *a, **kw: calls.append(True),
    )

    nudge_module.maybe_dispatch_daily_sweep()
    nudge_module.maybe_dispatch_daily_sweep()

    assert len(calls) == 1


def test_trigger_never_raises_on_redis_outage(monkeypatch):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    dispatched = []
    monkeypatch.setattr(rate_limit_module, "_redis_client", BrokenRedis())
    monkeypatch.setattr(
        nudge_module.send_subscription_nudges,
        "apply_async",
        lambda *a, **kw: dispatched.append(True),
    )

    nudge_module.maybe_dispatch_daily_sweep()

    assert dispatched == []


# --- amara_active flag ---------------------------------------------------------


def test_me_subscription_reports_active_when_no_plans(client, auth_user):
    _, headers = auth_user

    resp = client.get("/api/v1/auth/me/subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["amara_active"] is True
    assert resp.json()["status"] is None


def test_me_subscription_reports_inactive_for_unpaid(client, db_session, auth_user, platform_product):
    _, headers = auth_user

    resp = client.get("/api/v1/auth/me/subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["amara_active"] is False


def test_me_subscription_reports_active_for_paid(
    client, db_session, auth_user, platform_product
):
    user, headers = auth_user
    _make_active_sub(db_session, platform_product, user)

    resp = client.get("/api/v1/auth/me/subscription", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["amara_active"] is True
