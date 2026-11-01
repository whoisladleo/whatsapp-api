"""Atomic stock release on order resolution (P10).

Every stock-release path used to be check-then-read-modify-write: a FAILED
callback racing auto-cancel (or a dashboard cancel, or the reclose guard's
lazy retire of an expired hold) could both see PENDING and both restore the
same unit (+2 phantom stock). Releases now go through
crud.order.try_release_pending_order — the release-side mirror of
_try_claim_stock: a conditional UPDATE moves the order out of PENDING, so
only the transition winner restores, and the restore itself is a SQL
expression (quantity = quantity + N), not a read-modify-write.

Race-test approach follows tests/unit/test_stock_claim.py: true thread
concurrency is infeasible on this suite's SQLite setup (one shared
in-memory connection, Sessions not thread-safe), so the race is simulated
deterministically — the row is flipped out of PENDING behind the ORM
object's back (bulk UPDATE, synchronize_session=False, uncommitted), and
the path under test runs against the stale PENDING object exactly as a
loser of the real race would.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app.crud.order import OrderNotCancellableError, cancel_order, try_release_pending_order
from app.db.models import Customer, Order, OrderStatus, PaymentType, Product, User, WhatsAppAccount
from app.services.llm.helpers import _reclose_guard_note


@pytest.fixture
def owner(db_session):
    user = User(username="255700000950")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Release Shop", phone_number_id="RELEASE_PHONE", access_token="tok",
        user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711195001")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="release-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=owner.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def pending_order(db_session, customer, account, product):
    o = Order(
        customer_id=customer.id, account_id=account.id, product_id=product.id,
        quantity=1, agreed_price=45000, payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def _flip_row_out_of_pending(db_session, order):
    """Simulates a concurrent resolver committing first: the DB row leaves
    PENDING while this session's ORM object still shows PENDING (stale)."""
    db_session.query(Order).filter(Order.id == order.id).update(
        {Order.status: OrderStatus.CANCELLED}, synchronize_session=False
    )
    assert order.status == OrderStatus.PENDING  # stale, as a race loser sees it


def test_release_wins_transition_and_restores_stock(db_session, pending_order, product):
    won, restored_id = try_release_pending_order(db_session, pending_order, OrderStatus.CANCELLED)
    db_session.commit()

    assert won is True
    assert restored_id == product.id
    assert pending_order.status == OrderStatus.CANCELLED
    db_session.refresh(product)
    assert product.quantity == 6


def test_release_marks_failed_when_asked(db_session, pending_order, product):
    won, restored_id = try_release_pending_order(db_session, pending_order, OrderStatus.FAILED)
    db_session.commit()

    assert won is True
    assert restored_id == product.id
    db_session.refresh(pending_order)
    assert pending_order.status == OrderStatus.FAILED
    db_session.refresh(product)
    assert product.quantity == 6


def test_release_loses_race_restores_nothing(db_session, pending_order, product):
    _flip_row_out_of_pending(db_session, pending_order)

    won, restored_id = try_release_pending_order(db_session, pending_order, OrderStatus.CANCELLED)

    assert won is False
    assert restored_id is None
    db_session.refresh(product)
    assert product.quantity == 5


def test_release_for_subscription_restores_no_stock(db_session, pending_order, product):
    product.is_subscription = True
    db_session.commit()

    won, restored_id = try_release_pending_order(db_session, pending_order, OrderStatus.CANCELLED)
    db_session.commit()

    assert won is True
    assert restored_id is None
    db_session.refresh(product)
    assert product.quantity == 5


def test_release_without_product_link_still_transitions(db_session, customer):
    order = Order(customer_id=customer.id, status=OrderStatus.PENDING, agreed_price=1000)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    won, restored_id = try_release_pending_order(db_session, order, OrderStatus.CANCELLED)
    db_session.commit()

    assert won is True
    assert restored_id is None
    db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED


def test_release_handles_null_quantity(db_session, pending_order, product):
    db_session.query(Product).filter(Product.id == product.id).update(
        {Product.quantity: None}, synchronize_session=False
    )
    db_session.commit()
    db_session.refresh(product)
    assert product.quantity is None

    won, restored_id = try_release_pending_order(db_session, pending_order, OrderStatus.CANCELLED)
    db_session.commit()

    assert won is True
    assert restored_id == product.id
    db_session.refresh(product)
    assert product.quantity == 1


def test_release_does_not_commit(db_session, pending_order, product):
    try_release_pending_order(db_session, pending_order, OrderStatus.CANCELLED)

    db_session.rollback()
    db_session.refresh(pending_order)
    db_session.refresh(product)
    assert pending_order.status == OrderStatus.PENDING
    assert product.quantity == 5


def test_cancel_order_loses_race_raises_without_phantom_restore(
    db_session, pending_order, product
):
    _flip_row_out_of_pending(db_session, pending_order)

    with pytest.raises(OrderNotCancellableError):
        cancel_order(db_session, pending_order)

    db_session.refresh(product)
    assert product.quantity == 5


def test_reclose_retire_loses_race_restores_nothing(
    db_session, customer, account, product, pending_order, monkeypatch
):
    # Unlike the paths above, the guard re-queries with a status=PENDING
    # filter, so the concurrent transition must land between its read and
    # its retire — simulated by flipping inside the release call itself.
    import app.services.llm.helpers as llm_helpers

    pending_order.reserved_until = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()
    real_release = getattr(llm_helpers, "try_release_pending_order", None)

    def _flip_then_release(db, order, to_status):
        db.query(Order).filter(Order.id == order.id).update(
            {Order.status: OrderStatus.CANCELLED}, synchronize_session=False
        )
        return real_release(db, order, to_status)

    monkeypatch.setattr(
        llm_helpers, "try_release_pending_order", _flip_then_release, raising=False
    )

    note = _reclose_guard_note(
        db_session, customer.id, product, Order.account_id == account.id, "en"
    )

    assert note is None  # no live blocker either way — close fresh
    db_session.refresh(product)
    assert product.quantity == 5
