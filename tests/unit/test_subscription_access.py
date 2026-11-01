from datetime import datetime, timedelta

import pytest

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
from app.services import subscription_access

PRODUCT_SLUG = "taskflow-os-subscription"


@pytest.fixture
def platform_owner(db_session):
    """Taskflow itself — the merchant who owns the platform subscription product."""
    u = User(username="255700009000")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def platform_product(db_session, platform_owner):
    p = Product(
        slug=PRODUCT_SLUG, name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def merchant(db_session):
    """A gated Taskflow merchant — logs in with this phone number."""
    u = User(username="255711220000")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def merchant_customer(db_session, merchant):
    """The Customer row representing that same merchant as a *buyer* of the
    platform subscription — phone-keyed, matching merchant.username."""
    c = Customer(phone_number=merchant.username)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _make_subscription(db_session, platform_product, merchant_customer, **overrides):
    order = Order(
        customer_id=merchant_customer.id, product_id=platform_product.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    sub = ProductSubscription(
        order_id=order.id, customer_id=merchant_customer.id, product_id=platform_product.id,
        monthly_amount=100000, **overrides,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


# --- is_merchant_allowed_ai ----------------------------------------------------


def test_allowed_when_no_plan_exists(db_session, merchant):
    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_allowed_when_no_product_flagged_as_plan(db_session, merchant, platform_product):
    # A subscription-shaped product that isn't a plan SKU gates nobody —
    # the flag, not the product's existence, switches gating on.
    platform_product.is_platform_plan = False
    db_session.commit()

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_allowed_when_merchant_is_the_platform_product_seller(db_session, platform_owner, platform_product):
    """
    The merchant who sells the platform subscription product (Taskflow
    itself) can never have a subscription to it, and must never be gated by
    it — this is the account that most needs to keep responding, since it's
    the one pitching new merchants. No ProductSubscription row exists here
    at all, which would otherwise deny.
    """
    assert subscription_access.is_merchant_allowed_ai(db_session, platform_owner) is True


def test_platform_product_seller_exemption_does_not_apply_to_other_merchants(
    db_session, merchant, platform_product
):
    """Sanity check the exemption is scoped to product.merchant_id, not a
    blanket bypass — an unrelated merchant is still gated normally."""
    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_denied_when_no_customer_match_at_all(db_session, merchant, platform_product):
    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_denied_when_customer_exists_but_no_subscription(db_session, merchant, merchant_customer, platform_product):
    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_allowed_when_active(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_allowed_when_pending_first_payment(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.PENDING_PAYMENT)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_denied_when_pending_first_payment_is_stale(db_session, merchant, merchant_customer, platform_product):
    # An abandoned Pay Now attempt never resolves on its own — without an
    # age bound, PENDING_PAYMENT granted access forever (#110).
    sub = _make_subscription(
        db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.PENDING_PAYMENT
    )
    sub.created_at = datetime.utcnow() - timedelta(
        hours=subscription_access.PENDING_PAYMENT_ALLOW_HOURS + 1
    )
    db_session.commit()

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_denied_when_first_payment_failed(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.PAYMENT_FAILED)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_denied_when_cancelled(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.CANCELLED)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_allowed_when_past_due_within_grace_period(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(
        db_session, platform_product, merchant_customer,
        status=ProductSubscriptionStatus.PAST_DUE, past_due_since=datetime.utcnow() - timedelta(days=3),
    )

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_denied_when_past_due_beyond_grace_period(db_session, merchant, merchant_customer, platform_product):
    _make_subscription(
        db_session, platform_product, merchant_customer,
        status=ProductSubscriptionStatus.PAST_DUE, past_due_since=datetime.utcnow() - timedelta(days=8),
    )

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_allowed_when_past_due_but_grace_clock_not_yet_set(db_session, merchant, merchant_customer, platform_product):
    """Defensive: past_due_since should always be set alongside PAST_DUE by
    product_subscription.py, but if it somehow isn't, don't deny outright."""
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.PAST_DUE)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_matches_via_facebook_login_users_phone_number_field(db_session, platform_product):
    fb_merchant = User(username=None, facebook_id="fb123", phone_number="255711330000")
    db_session.add(fb_merchant)
    db_session.commit()
    db_session.refresh(fb_merchant)

    customer = Customer(phone_number="255711330000")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    _make_subscription(db_session, platform_product, customer, status=ProductSubscriptionStatus.ACTIVE)

    assert subscription_access.is_merchant_allowed_ai(db_session, fb_merchant) is True


def test_denied_for_facebook_login_user_with_no_phone_set(db_session, platform_product):
    fb_merchant = User(username=None, facebook_id="fb456")
    db_session.add(fb_merchant)
    db_session.commit()
    db_session.refresh(fb_merchant)

    assert subscription_access.is_merchant_allowed_ai(db_session, fb_merchant) is False


# --- sync_ai_access_for_merchant -----------------------------------------------


@pytest.fixture
def whatsapp_account(db_session, merchant):
    a = WhatsAppAccount(
        name="Merchant Shop", phone_number_id="MPHONE1", access_token="tok",
        user_id=merchant.id, ai_enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def instagram_account(db_session, merchant):
    a = InstagramAccount(
        ig_business_id="ig_merchant_1", access_token="ig-tok", username="merchant_shop",
        user_id=merchant.id, ai_enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


def test_sync_disables_ai_when_denied(db_session, merchant, platform_product, whatsapp_account, instagram_account):
    subscription_access.sync_ai_access_for_merchant(db_session, merchant)

    db_session.refresh(whatsapp_account)
    db_session.refresh(instagram_account)
    assert whatsapp_account.ai_enabled is False
    assert instagram_account.ai_enabled is False


def test_sync_enables_ai_when_allowed(
    db_session, merchant, merchant_customer, platform_product, whatsapp_account, instagram_account
):
    whatsapp_account.ai_enabled = False
    instagram_account.ai_enabled = False
    db_session.commit()
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    subscription_access.sync_ai_access_for_merchant(db_session, merchant)

    db_session.refresh(whatsapp_account)
    db_session.refresh(instagram_account)
    assert whatsapp_account.ai_enabled is True
    assert instagram_account.ai_enabled is True


def test_sync_skips_disconnected_instagram_account(db_session, merchant, platform_product, instagram_account):
    instagram_account.access_token = None
    instagram_account.ai_enabled = False
    db_session.commit()

    subscription_access.sync_ai_access_for_merchant(db_session, merchant)

    db_session.refresh(instagram_account)
    assert instagram_account.ai_enabled is False
    assert instagram_account.access_token is None


def test_sync_is_a_no_op_without_any_plan(db_session, merchant, whatsapp_account):
    subscription_access.sync_ai_access_for_merchant(db_session, merchant)

    db_session.refresh(whatsapp_account)
    assert whatsapp_account.ai_enabled is True


def test_sync_enables_ai_for_platform_product_seller_with_no_subscription(
    db_session, platform_owner, platform_product
):
    """
    End-to-end version of test_allowed_when_merchant_is_the_platform_product_seller
    — confirms the exemption actually reaches sync_ai_access_for_merchant
    (the path upsert_account/create_whatsapp_account call on every connect),
    not just is_merchant_allowed_ai in isolation. The seller has no
    ProductSubscription row at all (can't subscribe to themselves), which
    would deny any other merchant — their own accounts must still end up
    ai_enabled=True.
    """
    whatsapp_account = WhatsAppAccount(
        name="Taskflow", phone_number_id="PLATFORM_PHONE", access_token="tok",
        user_id=platform_owner.id, ai_enabled=False,
    )
    instagram_account = InstagramAccount(
        ig_business_id="ig_platform_1", access_token="ig-tok", username="taskflow_os",
        user_id=platform_owner.id, ai_enabled=False,
    )
    db_session.add_all([whatsapp_account, instagram_account])
    db_session.commit()

    subscription_access.sync_ai_access_for_merchant(db_session, platform_owner)

    db_session.refresh(whatsapp_account)
    db_session.refresh(instagram_account)
    assert whatsapp_account.ai_enabled is True
    assert instagram_account.ai_enabled is True


# --- multiple plan SKUs (#90) --------------------------------------------------


@pytest.fixture
def pro_plan(db_session, platform_owner):
    p = Product(
        slug="taskflow-os-pro", name="Taskflow OS Pro", max_price=200000, min_price=200000,
        quantity=999999, merchant_id=platform_owner.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=10000,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_allowed_by_any_allowing_plan_subscription(db_session, merchant, merchant_customer, platform_product, pro_plan):
    # Standard lapsed, Pro active — access holds via Pro.
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.CANCELLED)
    _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is True


def test_denied_when_every_plan_subscription_lapsed(db_session, merchant, merchant_customer, platform_product, pro_plan):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.CANCELLED)
    _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.PAYMENT_FAILED)

    assert subscription_access.is_merchant_allowed_ai(db_session, merchant) is False


def test_find_platform_subscriptions_spans_plans_newest_first(
    db_session, merchant, merchant_customer, platform_product, pro_plan
):
    old = _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)
    new = _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    subs = subscription_access.find_platform_subscriptions(db_session, merchant)

    assert [s.id for s in subs] == [new.id, old.id]


def test_plan_reply_limit_is_highest_granting_plan(db_session, merchant, merchant_customer, platform_product, pro_plan):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)
    _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    assert subscription_access.plan_reply_limit(db_session, merchant) == 10000


def test_plan_reply_limit_ignores_lapsed_subscriptions(db_session, merchant, merchant_customer, platform_product, pro_plan):
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)
    _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.CANCELLED)

    assert subscription_access.plan_reply_limit(db_session, merchant) == 2000


def test_plan_reply_limit_unlimited_when_a_granting_plan_has_no_limit(
    db_session, merchant, merchant_customer, platform_product, pro_plan
):
    pro_plan.monthly_reply_limit = None
    db_session.commit()
    _make_subscription(db_session, platform_product, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)
    _make_subscription(db_session, pro_plan, merchant_customer, status=ProductSubscriptionStatus.ACTIVE)

    assert subscription_access.plan_reply_limit(db_session, merchant) is None


def test_plan_reply_limit_unlimited_without_any_plan(db_session, merchant):
    assert subscription_access.plan_reply_limit(db_session, merchant) is None


def test_plan_reply_limit_unlimited_for_seller_or_unsubscribed(db_session, merchant, platform_owner, platform_product):
    # No subscription rows at all here — the seller is exempt, and an
    # unsubscribed merchant is denied access outright, so neither gets a
    # meter (None would also mean "unlimited" for the seller's Amara).
    assert subscription_access.plan_reply_limit(db_session, merchant) is None
    assert subscription_access.plan_reply_limit(db_session, platform_owner) is None
