from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models import (
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
from app.services import product_subscription

PRODUCT_SLUG = "taskflow-os-subscription"


@pytest.fixture(autouse=True)
def _configure_platform_product(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", PRODUCT_SLUG)


@pytest.fixture
def platform_owner(db_session):
    u = User(username="255700009100")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def platform_product(db_session, platform_owner):
    p = Product(
        slug=PRODUCT_SLUG, name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def gated_merchant(db_session):
    u = User(username="255711221000")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def gated_merchant_customer(db_session, gated_merchant):
    c = Customer(phone_number=gated_merchant.username)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def gated_merchant_whatsapp_account(db_session, gated_merchant):
    a = WhatsAppAccount(
        name="Gated Shop", phone_number_id="GATEDPHONE1", access_token="tok",
        user_id=gated_merchant.id, ai_enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def gated_merchant_instagram_account(db_session, gated_merchant):
    a = InstagramAccount(
        ig_business_id="ig_gated_1", access_token="ig-tok", username="gated_shop",
        user_id=gated_merchant.id, ai_enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def platform_order(db_session, gated_merchant_customer, platform_product):
    o = Order(
        customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture
def pending_subscription(db_session, platform_order, gated_merchant_customer, platform_product):
    sub = ProductSubscription(
        order_id=platform_order.id, customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


# --- activate_after_first_payment / mark_first_payment_failed -----------------


def test_activate_first_payment_enables_ai_for_gated_merchant(
    db_session, pending_subscription, gated_merchant_whatsapp_account, gated_merchant_instagram_account
):
    gated_merchant_whatsapp_account.ai_enabled = False
    gated_merchant_instagram_account.ai_enabled = False
    db_session.commit()

    product_subscription.activate_after_first_payment(db_session, pending_subscription)

    db_session.refresh(gated_merchant_whatsapp_account)
    db_session.refresh(gated_merchant_instagram_account)
    assert gated_merchant_whatsapp_account.ai_enabled is True
    assert gated_merchant_instagram_account.ai_enabled is True


def test_mark_first_payment_failed_keeps_ai_disabled(
    db_session, pending_subscription, gated_merchant_whatsapp_account
):
    product_subscription.mark_first_payment_failed(db_session, pending_subscription)

    db_session.refresh(gated_merchant_whatsapp_account)
    assert gated_merchant_whatsapp_account.ai_enabled is False


# --- handle_renewal_payment_result ---------------------------------------------


async def test_renewal_completed_clears_past_due_and_enables_ai(
    db_session, platform_order, gated_merchant_customer, platform_product, gated_merchant_whatsapp_account, monkeypatch
):
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.charge_renewal.apply_async", lambda *a, **kw: None
    )
    sub = ProductSubscription(
        order_id=platform_order.id, customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=2,
        past_due_since=datetime.utcnow() - timedelta(days=9),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    gated_merchant_whatsapp_account.ai_enabled = False
    db_session.commit()

    await product_subscription.handle_renewal_payment_result(db_session, sub, "COMPLETED", "TXN-1")

    assert sub.past_due_since is None
    db_session.refresh(gated_merchant_whatsapp_account)
    assert gated_merchant_whatsapp_account.ai_enabled is True


async def test_renewal_failed_first_time_sets_past_due_since_and_schedules_grace_check(
    db_session, platform_order, gated_merchant_customer, platform_product, gated_merchant_whatsapp_account, monkeypatch
):
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.charge_renewal.apply_async", lambda *a, **kw: None
    )
    grace_calls = []
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.check_past_due_grace_expired.apply_async",
        lambda *, args, **kw: grace_calls.append({"args": args, "kwargs": kw}),
    )

    async def fake_send_text(self, to, body, **kwargs):
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    sub = ProductSubscription(
        order_id=platform_order.id, customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000, retry_count=0,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-2")

    assert sub.past_due_since is not None
    assert len(grace_calls) == 1
    assert grace_calls[0]["args"] == [sub.id]
    assert grace_calls[0]["kwargs"]["countdown"] == 7 * 86400

    # Still within grace — access stays on.
    db_session.refresh(gated_merchant_whatsapp_account)
    assert gated_merchant_whatsapp_account.ai_enabled is True


async def test_renewal_failed_second_time_does_not_reset_past_due_since_or_reschedule_grace_check(
    db_session, platform_order, gated_merchant_customer, platform_product, monkeypatch
):
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.charge_renewal.apply_async", lambda *a, **kw: None
    )
    grace_calls = []
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.check_past_due_grace_expired.apply_async",
        lambda *, args, **kw: grace_calls.append(args),
    )

    async def fake_send_text(self, to, body, **kwargs):
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    original_past_due = datetime.utcnow() - timedelta(days=2)
    sub = ProductSubscription(
        order_id=platform_order.id, customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=1,
        past_due_since=original_past_due,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-3")

    assert sub.retry_count == 2
    assert sub.past_due_since == original_past_due
    assert grace_calls == []


async def test_renewal_cancelled_after_max_retries_disables_ai(
    db_session, platform_order, gated_merchant_customer, platform_product, gated_merchant_whatsapp_account, monkeypatch
):
    async def fake_send_text(self, to, body, **kwargs):
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    sub = ProductSubscription(
        order_id=platform_order.id, customer_id=gated_merchant_customer.id, product_id=platform_product.id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000,
        retry_count=product_subscription.MAX_RENEWAL_RETRIES,
        past_due_since=datetime.utcnow() - timedelta(days=14),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-4")

    assert sub.status == ProductSubscriptionStatus.CANCELLED
    db_session.refresh(gated_merchant_whatsapp_account)
    assert gated_merchant_whatsapp_account.ai_enabled is False


# --- unrelated (non-platform) subscriptions never affect ai_enabled -----------


async def test_non_platform_subscription_never_touches_any_users_ai_enabled(
    db_session, gated_merchant, gated_merchant_whatsapp_account, monkeypatch
):
    """A random merchant selling their own recurring product to their own
    customers must never accidentally gate some unrelated Taskflow user's
    Amara just because phone numbers happen to collide."""
    other_merchant = User(username="255700009200")
    db_session.add(other_merchant)
    db_session.commit()
    db_session.refresh(other_merchant)

    other_product = Product(
        slug="meal-plan", name="Monthly Meal Plan", max_price=50000, min_price=50000,
        quantity=999999, merchant_id=other_merchant.id, is_subscription=True,
    )
    db_session.add(other_product)
    db_session.commit()
    db_session.refresh(other_product)

    # This customer's phone happens to match gated_merchant's own login phone.
    customer = Customer(phone_number=gated_merchant.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(
        customer_id=customer.id, product_id=other_product.id,
        quantity=1, agreed_price=50000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=other_product.id, monthly_amount=50000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    product_subscription.mark_first_payment_failed(db_session, sub)

    # gated_merchant has no platform subscription at all, so if this had
    # incorrectly synced access it would have been denied (False) — confirm
    # it was left alone at its fixture default instead.
    db_session.refresh(gated_merchant_whatsapp_account)
    assert gated_merchant_whatsapp_account.ai_enabled is True
