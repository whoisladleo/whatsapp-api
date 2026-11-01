import pytest

import app.tasks.order_reminders as order_reminders
from app.db.models import Customer, Order, OrderStatus, PaymentType, Product, User, WhatsAppAccount


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """
    order_reminders tasks call the module-level SessionLocal directly (they
    aren't FastAPI routes, so get_db's dependency override doesn't apply).
    Left unpatched, calling a task would open a real connection to whatever
    Postgres server is configured in the environment/.env instead of the
    in-memory test database — redirect it to the same session the test uses.
    """
    monkeypatch.setattr(order_reminders, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000800")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Reminder Shop", phone_number_id="ORDER_REMINDER_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="reminder-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=owner.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711111111")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def pending_order(db_session, customer, account, product):
    order = Order(
        customer_id=customer.id,
        account_id=account.id,
        product_id=product.id,
        quantity=1,
        status=OrderStatus.PENDING,
        agreed_price=45000,
        payment_type=PaymentType.FULL,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


@pytest.fixture
def pending_order_no_account(db_session, customer):
    order = Order(customer_id=customer.id, status=OrderStatus.PENDING, agreed_price=45000)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def _mock_send_text(monkeypatch, ok=True):
    async def fake(self, to, body, preview_url=False):
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.reminder"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake)


def _mock_send_template(monkeypatch, ok=True, calls=None):
    async def fake(self, to, name, language_code="en_US", components=None):
        if calls is not None:
            calls.append({"to": to, "name": name, "language_code": language_code, "components": components})
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", fake)


# --- send_soft_nudge ---


def test_send_soft_nudge_missing_order():
    result = order_reminders.send_soft_nudge(999999)

    assert result == "Order not found or not pending."


def test_send_soft_nudge_non_pending_order(db_session, pending_order):
    pending_order.status = OrderStatus.PAID
    db_session.commit()

    result = order_reminders.send_soft_nudge(pending_order.id)

    assert result == "Order not found or not pending."


def test_send_soft_nudge_no_account(pending_order_no_account):
    result = order_reminders.send_soft_nudge(pending_order_no_account.id)

    assert result == "No account or customer to notify."


def test_send_soft_nudge_sends_and_logs(db_session, pending_order, customer, monkeypatch):
    _mock_send_text(monkeypatch)
    from app.db.models import Message

    result = order_reminders.send_soft_nudge(pending_order.id)

    assert result == "Success"
    logged = (
        db_session.query(Message)
        .filter(Message.to_phone == customer.phone_number, Message.status == "sent")
        .first()
    )
    assert logged is not None
    assert "reminder" in logged.body.lower()


def test_send_soft_nudge_send_failure_still_logs(db_session, pending_order, monkeypatch):
    _mock_send_text(monkeypatch, ok=False)
    from app.db.models import Message

    result = order_reminders.send_soft_nudge(pending_order.id)

    assert result == "Success"
    logged = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.status == "failed")
        .first()
    )
    assert logged is not None


# --- send_scarcity_nudge ---


def test_send_scarcity_nudge_missing_order():
    result = order_reminders.send_scarcity_nudge(999999)

    assert result == "Order not found or not pending."


def test_send_scarcity_nudge_no_account(pending_order_no_account):
    result = order_reminders.send_scarcity_nudge(pending_order_no_account.id)

    assert result == "No account or customer to notify."


def test_send_scarcity_nudge_sends_template(db_session, pending_order, monkeypatch):
    calls = []
    _mock_send_template(monkeypatch, calls=calls)
    from app.db.models import Message

    result = order_reminders.send_scarcity_nudge(pending_order.id)

    assert result == "Success"
    logged = db_session.query(Message).filter(Message.status == "sent").first()
    assert logged is not None
    assert "scarcity nudge" in logged.body

    assert len(calls) == 1
    from app.core.config import settings

    assert calls[0]["name"] == settings.WHATSAPP_REMINDER_TEMPLATE_NAME
    assert calls[0]["language_code"] == settings.WHATSAPP_REMINDER_TEMPLATE_LANGUAGE
    body_params = calls[0]["components"][0]["parameters"]
    assert body_params[0]["text"] == "Shoes"
    assert body_params[1]["text"] == "45,000"


def test_send_scarcity_nudge_uses_customers_classified_language(db_session, pending_order, customer, monkeypatch):
    customer.language = "sw"
    db_session.commit()

    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    result = order_reminders.send_scarcity_nudge(pending_order.id)

    assert result == "Success"
    assert calls[0]["language_code"] == "sw"


def test_send_scarcity_nudge_falls_back_to_generic_name_when_no_product(db_session, customer, account, monkeypatch):
    """order.product can be None (nullable product_id) — must not crash
    building the template parameters."""
    order = Order(
        customer_id=customer.id, account_id=account.id,
        quantity=1, status=OrderStatus.PENDING, agreed_price=20000, payment_type=PaymentType.FULL,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    result = order_reminders.send_scarcity_nudge(order.id)

    assert result == "Success"
    body_params = calls[0]["components"][0]["parameters"]
    assert body_params[0]["text"] == "your order"
    assert body_params[1]["text"] == "20,000"


def test_send_scarcity_nudge_send_failure_still_logs(db_session, pending_order, monkeypatch):
    _mock_send_template(monkeypatch, ok=False)
    from app.db.models import Message

    result = order_reminders.send_scarcity_nudge(pending_order.id)

    assert result == "Success"
    logged = db_session.query(Message).filter(Message.status == "failed").first()
    assert logged is not None


# --- auto_cancel_order ---


def test_auto_cancel_order_missing_order():
    result = order_reminders.auto_cancel_order(999999)

    assert result == "Order not found or not pending."


def test_auto_cancel_order_non_pending_order_untouched(db_session, pending_order):
    pending_order.status = OrderStatus.PAID
    db_session.commit()
    order_id = pending_order.id

    result = order_reminders.auto_cancel_order(order_id)

    reloaded = db_session.query(Order).filter(Order.id == order_id).first()
    assert result == "Order not found or not pending."
    assert reloaded.status == OrderStatus.PAID


def test_auto_cancel_order_cancels_and_releases_stock(db_session, pending_order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    order_id = pending_order.id
    product_id = product.id
    stock_before = product.quantity

    result = order_reminders.auto_cancel_order(order_id)

    reloaded_order = db_session.query(Order).filter(Order.id == order_id).first()
    reloaded_product = db_session.query(Product).filter(Product.id == product_id).first()
    assert result == "Success"
    assert reloaded_order.status == OrderStatus.CANCELLED
    assert reloaded_product.quantity == stock_before + 1


def test_auto_cancel_subscription_order_restores_no_stock(db_session, pending_order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    product.is_subscription = True
    product.quantity = 0
    db_session.commit()
    order_id = pending_order.id
    product_id = product.id

    result = order_reminders.auto_cancel_order(order_id)

    assert result == "Success"
    reloaded_order = db_session.query(Order).filter(Order.id == order_id).first()
    reloaded_product = db_session.query(Product).filter(Product.id == product_id).first()
    assert reloaded_order.status == OrderStatus.CANCELLED
    assert reloaded_product.quantity == 0


def test_auto_cancel_order_without_product_link_still_cancels(db_session, pending_order_no_account):
    result = order_reminders.auto_cancel_order(pending_order_no_account.id)

    reloaded = db_session.query(Order).filter(Order.id == pending_order_no_account.id).first()
    assert result == "Success"
    assert reloaded.status == OrderStatus.CANCELLED


def test_auto_cancel_order_notify_failure_still_logs(db_session, pending_order, monkeypatch):
    from app.db.models import Message

    _mock_send_text(monkeypatch, ok=False)

    result = order_reminders.auto_cancel_order(pending_order.id)

    assert result == "Success"
    logged = db_session.query(Message).filter(Message.status == "failed").first()
    assert logged is not None


def test_auto_cancel_order_fails_linked_pending_subscription(db_session, pending_order, monkeypatch):
    # An abandoned first payment must resolve to PAYMENT_FAILED, never sit
    # in PENDING_PAYMENT granting access forever (#110).
    from app.db.models import ProductSubscription, ProductSubscriptionStatus

    _mock_send_text(monkeypatch)
    sub = ProductSubscription(
        order_id=pending_order.id, customer_id=pending_order.customer_id,
        product_id=pending_order.product_id,
        status=ProductSubscriptionStatus.PENDING_PAYMENT, monthly_amount=45000,
    )
    db_session.add(sub)
    db_session.commit()
    order_id, sub_id = pending_order.id, sub.id

    result = order_reminders.auto_cancel_order(order_id)

    assert result == "Success"
    reloaded = db_session.query(ProductSubscription).filter(ProductSubscription.id == sub_id).first()
    assert reloaded.status == ProductSubscriptionStatus.PAYMENT_FAILED


def test_auto_cancel_order_skips_fresh_reservation(db_session, pending_order):
    # A newer attempt refreshed the reservation after this firing was
    # scheduled (Pay Now reschedules on reuse) — this firing is stale.
    from datetime import datetime, timedelta

    pending_order.reserved_until = datetime.utcnow() + timedelta(hours=47)
    db_session.commit()
    order_id = pending_order.id

    result = order_reminders.auto_cancel_order(order_id)

    reloaded = db_session.query(Order).filter(Order.id == order_id).first()
    assert result == "Reservation still active."
    assert reloaded.status == OrderStatus.PENDING


def test_auto_cancel_loses_race_restores_no_stock(db_session, pending_order, product, monkeypatch):
    # P10: a FAILED callback (or dashboard cancel) that resolved the order
    # after this firing read it must win alone — the firing restores nothing.
    _mock_send_text(monkeypatch)
    db_session.query(Order).filter(Order.id == pending_order.id).update(
        {Order.status: OrderStatus.CANCELLED}, synchronize_session=False
    )
    assert pending_order.status == OrderStatus.PENDING  # stale, as a race loser sees it
    order_id, product_id = pending_order.id, product.id
    stock_before = product.quantity

    result = order_reminders.auto_cancel_order(order_id)

    assert result == "Order not found or not pending."
    reloaded_product = db_session.query(Product).filter(Product.id == product_id).first()
    db_session.refresh(reloaded_product)
    assert reloaded_product.quantity == stock_before
