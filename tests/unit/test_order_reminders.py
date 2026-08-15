import pytest

import app.tasks.order_reminders as order_reminders
from app.db.models import Customer, Order, OrderStatus


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
def pending_order(db_session):
    customer = Customer(phone_number="255711111111")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, status=OrderStatus.PENDING, agreed_price=45000)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_send_soft_nudge_missing_order():
    result = order_reminders.send_soft_nudge(999999)

    assert result == "Order not found or not pending."


def test_send_soft_nudge_non_pending_order(db_session, pending_order):
    pending_order.status = OrderStatus.PAID
    db_session.commit()

    result = order_reminders.send_soft_nudge(pending_order.id)

    assert result == "Order not found or not pending."


def test_send_soft_nudge_pending_order(pending_order):
    result = order_reminders.send_soft_nudge(pending_order.id)

    assert result == "Success"


def test_send_scarcity_nudge_missing_order():
    result = order_reminders.send_scarcity_nudge(999999)

    assert result == "Order not found or not pending."


def test_send_scarcity_nudge_pending_order(pending_order):
    result = order_reminders.send_scarcity_nudge(pending_order.id)

    assert result == "Success"


def test_auto_cancel_order_missing_order():
    result = order_reminders.auto_cancel_order(999999)

    assert result == "Order not found or not pending."


def test_auto_cancel_order_non_pending_order_untouched(db_session, pending_order):
    pending_order.status = OrderStatus.PAID
    db_session.commit()
    order_id = pending_order.id

    result = order_reminders.auto_cancel_order(order_id)

    # The task's `finally: db.close()` expunges pending_order from db_session's
    # identity map, so re-query rather than refresh() the stale instance.
    reloaded = db_session.query(Order).filter(Order.id == order_id).first()
    assert result == "Order not found or not pending."
    assert reloaded.status == OrderStatus.PAID


def test_auto_cancel_order_cancels_pending_order(db_session, pending_order):
    order_id = pending_order.id

    result = order_reminders.auto_cancel_order(order_id)

    reloaded = db_session.query(Order).filter(Order.id == order_id).first()
    assert result == "Success"
    assert reloaded.status == OrderStatus.CANCELLED
