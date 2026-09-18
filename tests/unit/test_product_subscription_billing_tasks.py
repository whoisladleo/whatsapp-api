import pytest

import app.tasks.product_subscription_billing as product_subscription_tasks
from app.db.models import (
    Customer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(product_subscription_tasks, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000900")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Sub Shop", phone_number_id="SUBPHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="sub-product", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711119999")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def order(db_session, account, product, customer):
    o = Order(
        customer_id=customer.id, product_id=product.id, account_id=account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def test_charge_renewal_pushes_payment_when_active(db_session, order, customer, monkeypatch):
    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = []

    async def fake_initiate(self, phone, amount, reference):
        calls.append((phone, amount, reference))
        return {}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate)

    product_subscription_tasks.charge_renewal(sub.id)

    assert calls == [(customer.phone_number, 100000, f"product-subscription-renewal-{sub.id}")]


def test_charge_renewal_pushes_payment_when_past_due(db_session, order, customer, monkeypatch):
    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=3,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = []

    async def fake_initiate(self, phone, amount, reference):
        calls.append((phone, amount, reference))
        return {}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate)

    product_subscription_tasks.charge_renewal(sub.id)

    assert len(calls) == 1


def test_charge_renewal_skips_when_cancelled(db_session, order, customer, monkeypatch):
    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.CANCELLED, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = []

    async def fake_initiate(self, phone, amount, reference):
        calls.append((phone, amount, reference))
        return {}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate)

    product_subscription_tasks.charge_renewal(sub.id)

    assert calls == []


def test_charge_renewal_missing_subscription_does_not_crash(db_session):
    product_subscription_tasks.charge_renewal(999999)  # no exception


def test_charge_renewal_logs_and_swallows_payment_push_failure(db_session, order, customer, monkeypatch):
    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    async def fake_initiate_fails(self, phone, amount, reference):
        raise RuntimeError("Tunzaa is down")

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate_fails)

    product_subscription_tasks.charge_renewal(sub.id)  # no exception


# --- check_past_due_grace_expired ----------------------------------------------


@pytest.fixture
def self_paying_merchant_order(db_session, owner, account, product):
    """The gated merchant IS the customer here — a Customer row whose phone
    matches `owner.username`, simulating a merchant who bought their own
    platform subscription with their login phone number."""
    customer = Customer(phone_number=owner.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    o = Order(
        customer_id=customer.id, product_id=product.id, account_id=account.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o, customer


def test_grace_expired_disables_ai_when_still_past_due(
    db_session, self_paying_merchant_order, account, monkeypatch
):
    from datetime import datetime, timedelta

    from app.core.config import settings

    order, customer = self_paying_merchant_order
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "sub-product")
    account.ai_enabled = True
    db_session.commit()

    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=3,
        past_due_since=datetime.utcnow() - timedelta(days=7),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    account_id = account.id
    product_subscription_tasks.check_past_due_grace_expired(sub.id)

    reloaded = db_session.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_id).first()
    assert reloaded.ai_enabled is False


def test_grace_expired_no_op_if_already_recovered(
    db_session, self_paying_merchant_order, account, monkeypatch
):
    from app.core.config import settings

    order, customer = self_paying_merchant_order
    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "sub-product")
    account.ai_enabled = True
    db_session.commit()

    sub = ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    account_id = account.id
    product_subscription_tasks.check_past_due_grace_expired(sub.id)

    reloaded = db_session.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_id).first()
    assert reloaded.ai_enabled is True


def test_grace_expired_missing_subscription_does_not_crash(db_session):
    product_subscription_tasks.check_past_due_grace_expired(999999)  # no exception
