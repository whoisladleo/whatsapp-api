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


def _mock_send_template(monkeypatch, ok=True):
    async def fake(self, to, name, language_code="en_US", components=None):
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
    _mock_send_template(monkeypatch)
    from app.db.models import Message

    result = order_reminders.send_scarcity_nudge(pending_order.id)

    assert result == "Success"
    logged = db_session.query(Message).filter(Message.status == "sent").first()
    assert logged is not None
    assert "scarcity nudge" in logged.body


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
