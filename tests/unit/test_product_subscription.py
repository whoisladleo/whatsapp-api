import pytest

from app.db.models import (
    Customer,
    InstagramAccount,
    InstagramCustomer,
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


def _mock_apply_async(monkeypatch):
    calls = []

    def fake(*, args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.charge_renewal.apply_async", fake
    )
    return calls


@pytest.fixture
def merchant(db_session):
    user = User(username="255700001000")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def whatsapp_account(db_session, merchant):
    acct = WhatsAppAccount(
        name="Sub Shop", phone_number_id="SUBPHONE2", access_token="tok", user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def instagram_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_sub_biz_1", access_token="ig-tok", username="sub_shop", user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def product(db_session, merchant):
    p = Product(
        slug="sub-product-2", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711118888")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def whatsapp_order(db_session, whatsapp_account, product, customer):
    o = Order(
        customer_id=customer.id, product_id=product.id, account_id=whatsapp_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture
def instagram_order(db_session, instagram_account, product, customer):
    o = Order(
        customer_id=customer.id, product_id=product.id, instagram_account_id=instagram_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture
def ig_customer(db_session, instagram_account):
    c = InstagramCustomer(ig_scoped_id="igsid_sub_1", username="ig_subscriber", account_id=instagram_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


# --- activate_after_first_payment / mark_first_payment_failed -----------------


def test_activate_after_first_payment_sets_active_and_schedules_renewal(
    db_session, whatsapp_order, customer, monkeypatch
):
    calls = _mock_apply_async(monkeypatch)
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    product_subscription.activate_after_first_payment(db_session, sub)

    assert sub.status == ProductSubscriptionStatus.ACTIVE
    assert sub.next_billing_at is not None
    assert calls[0]["args"] == [str(sub.id)]
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400


def test_mark_first_payment_failed_sets_payment_failed(db_session, whatsapp_order, customer):
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    product_subscription.mark_first_payment_failed(db_session, sub)

    assert sub.status == ProductSubscriptionStatus.PAYMENT_FAILED


# --- handle_renewal_payment_result --------------------------------------------


async def test_renewal_completed_reactivates_and_reschedules(
    db_session, whatsapp_order, customer, monkeypatch
):
    calls = _mock_apply_async(monkeypatch)
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=2,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "COMPLETED", "TXN-1")

    assert sub.status == ProductSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.tunzaa_transaction_id == "TXN-1"
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400


async def test_renewal_failure_retries_and_notifies_via_whatsapp(
    db_session, whatsapp_account, whatsapp_order, customer, monkeypatch
):
    calls = _mock_apply_async(monkeypatch)
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000, retry_count=0,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    customer_notices = []
    merchant_notices = []

    async def fake_send_text(self, to, body, **kwargs):
        if to == customer.phone_number:
            customer_notices.append(body)
        elif to == whatsapp_account.owner.username:
            merchant_notices.append(body)
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-2")

    assert sub.status == ProductSubscriptionStatus.PAST_DUE
    assert sub.retry_count == 1
    assert calls[0]["kwargs"]["countdown"] == product_subscription.RENEWAL_RETRY_DELAY_SECONDS
    assert len(customer_notices) == 1
    assert len(merchant_notices) == 1
    assert "1/10" in merchant_notices[0] or "attempt 1" in merchant_notices[0]


async def test_renewal_failure_cancels_after_max_retries(
    db_session, whatsapp_order, customer, monkeypatch
):
    calls = _mock_apply_async(monkeypatch)
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000,
        retry_count=product_subscription.MAX_RENEWAL_RETRIES,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    async def fake_send_text(self, to, body, **kwargs):
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-3")

    assert sub.status == ProductSubscriptionStatus.CANCELLED
    # No further renewal scheduled once cancelled.
    assert calls == []


async def test_renewal_notifies_via_instagram_dm_when_linked(
    db_session, instagram_account, instagram_order, customer, ig_customer, monkeypatch
):
    sub = ProductSubscription(
        order_id=instagram_order.id, customer_id=customer.id, product_id=instagram_order.product_id,
        instagram_customer_id=ig_customer.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000, retry_count=0,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    dm_calls = []

    async def fake_send_dm(self, ig_business_id, recipient_id, message):
        dm_calls.append((ig_business_id, recipient_id, message))
        return {"message_id": "mid_1"}

    whatsapp_calls = []

    async def fake_send_text(self, to, body, **kwargs):
        whatsapp_calls.append((to, body))
        return {}

    monkeypatch.setattr("app.core.instagram_provider.InstagramProvider.send_direct_message", fake_send_dm)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(
        "app.tasks.product_subscription_billing.charge_renewal.apply_async", lambda *a, **kw: None
    )

    await product_subscription.handle_renewal_payment_result(db_session, sub, "FAILED", "TXN-4")

    assert len(dm_calls) == 1
    assert dm_calls[0][0] == instagram_account.ig_business_id
    assert dm_calls[0][1] == ig_customer.ig_scoped_id
    # Enforced single channel — no WhatsApp attempt for an Instagram-linked subscription.
    assert whatsapp_calls == []


async def test_renewal_pending_status_is_ignored(db_session, whatsapp_order, customer, monkeypatch):
    calls = _mock_apply_async(monkeypatch)
    sub = ProductSubscription(
        order_id=whatsapp_order.id, customer_id=customer.id, product_id=whatsapp_order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    await product_subscription.handle_renewal_payment_result(db_session, sub, "PENDING", None)

    assert sub.status == ProductSubscriptionStatus.ACTIVE
    assert calls == []


# --- get_or_create_pending_subscription ----------------------------------------


def test_get_or_create_pending_subscription_creates_when_none_exists(
    db_session, whatsapp_order, product, customer
):
    sub = product_subscription.get_or_create_pending_subscription(
        db_session, whatsapp_order, product, customer.id
    )

    assert sub.order_id == whatsapp_order.id
    assert sub.customer_id == customer.id
    assert sub.product_id == product.id
    assert sub.status == ProductSubscriptionStatus.PENDING_PAYMENT
    assert db_session.query(ProductSubscription).count() == 1


def test_get_or_create_pending_subscription_reuses_existing_pending_row(
    db_session, whatsapp_account, product, customer
):
    """
    The exact bug this fixes: a customer retrying before their first
    payment ever confirms (abandoned Tunzaa prompt, repeated Pay Now
    clicks, re-negotiating a later chat) must not accumulate one orphaned
    PENDING_PAYMENT row per attempt.
    """
    first_order = Order(
        customer_id=customer.id, product_id=product.id, account_id=whatsapp_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(first_order)
    db_session.commit()
    db_session.refresh(first_order)
    first_sub = product_subscription.get_or_create_pending_subscription(
        db_session, first_order, product, customer.id
    )

    second_order = Order(
        customer_id=customer.id, product_id=product.id, account_id=whatsapp_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(second_order)
    db_session.commit()
    db_session.refresh(second_order)
    second_sub = product_subscription.get_or_create_pending_subscription(
        db_session, second_order, product, customer.id
    )

    assert second_sub.id == first_sub.id
    assert second_sub.order_id == second_order.id
    assert db_session.query(ProductSubscription).count() == 1


def test_get_or_create_pending_subscription_does_not_reuse_active_row(
    db_session, whatsapp_order, product, customer
):
    """An ACTIVE/PAST_DUE/CANCELLED/PAYMENT_FAILED subscription is real
    billing history, not an abandoned attempt — only PENDING_PAYMENT gets
    deduped."""
    db_session.add(
        ProductSubscription(
            order_id=whatsapp_order.id, customer_id=customer.id, product_id=product.id,
            status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        )
    )
    db_session.commit()

    new_order = Order(
        customer_id=customer.id, product_id=product.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(new_order)
    db_session.commit()
    db_session.refresh(new_order)

    sub = product_subscription.get_or_create_pending_subscription(db_session, new_order, product, customer.id)

    assert sub.order_id == new_order.id
    assert db_session.query(ProductSubscription).count() == 2


def test_get_or_create_pending_subscription_sets_instagram_customer_id_on_reuse(
    db_session, whatsapp_account, product, customer, ig_customer
):
    first_order = Order(
        customer_id=customer.id, product_id=product.id, account_id=whatsapp_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(first_order)
    db_session.commit()
    db_session.refresh(first_order)
    product_subscription.get_or_create_pending_subscription(db_session, first_order, product, customer.id)

    second_order = Order(
        customer_id=customer.id, product_id=product.id, account_id=whatsapp_account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(second_order)
    db_session.commit()
    db_session.refresh(second_order)

    sub = product_subscription.get_or_create_pending_subscription(
        db_session, second_order, product, customer.id, instagram_customer_id=ig_customer.id
    )

    assert sub.instagram_customer_id == ig_customer.id
    assert db_session.query(ProductSubscription).count() == 1
