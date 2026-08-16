import asyncio

from app.api.v1.orders import _notify_customer_of_cancellation
from app.db.models import Customer, Order


def test_notify_skips_when_order_has_no_account_id(db_session):
    customer = Customer(phone_number="255700000900")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, account_id=None)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    # Should return cleanly without attempting any WhatsApp send.
    asyncio.run(_notify_customer_of_cancellation(db_session, order))


def test_notify_skips_when_order_has_no_customer(db_session):
    order = Order(customer_id=None, account_id=None)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    asyncio.run(_notify_customer_of_cancellation(db_session, order))


def test_notify_skips_when_account_not_found(db_session):
    customer = Customer(phone_number="255700000901")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, account_id=999999)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    asyncio.run(_notify_customer_of_cancellation(db_session, order))
