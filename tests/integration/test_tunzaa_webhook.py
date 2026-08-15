import pytest

from app.db.models import Customer, Installment, InstallmentStatus, Order, OrderStatus

URL = "/api/v1/tunzaa/tunzaa-webhook"


@pytest.fixture
def order(db_session):
    customer = Customer(phone_number="255711112222")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, agreed_price=45000, status=OrderStatus.PENDING)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_invalid_json_payload_returns_400(client):
    resp = client.post(URL, content=b"not-json", headers={"Content-Type": "application/json"})

    assert resp.status_code == 400


def test_missing_reference_is_ignored(client):
    resp = client.post(URL, json={"event": "PAYMENT_COMPLETE"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "No reference provided"}


def test_reference_without_dash_is_ignored(client, order):
    resp = client.post(URL, json={"event": "PAYMENT_COMPLETE", "reference": "noreference"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}


def test_reference_for_nonexistent_order_is_ignored(client):
    resp = client.post(URL, json={"event": "PAYMENT_COMPLETE", "reference": "order-999999"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}


def test_payment_complete_marks_order_paid(client, db_session, order):
    resp = client.post(URL, json={"event": "PAYMENT_COMPLETE", "reference": f"order-{order.id}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Order marked as PAID"}
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


def test_first_installment_success_marks_order_paid(client, db_session, order):
    resp = client.post(
        URL, json={"event": "FIRST_INSTALLMENT_SUCCESS", "reference": f"order-{order.id}"}
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


def test_payment_complete_updates_matching_installment(client, db_session, order):
    installment = Installment(
        order_id=order.id,
        tunzaa_installment_id="inst-1",
        amount_due=15000,
        due_date=order.created_at,
        status=InstallmentStatus.PENDING,
    )
    db_session.add(installment)
    db_session.commit()
    db_session.refresh(installment)

    resp = client.post(
        URL,
        json={
            "event": "PAYMENT_COMPLETE",
            "reference": f"order-{order.id}",
            "installment_id": "inst-1",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(installment)
    assert installment.status == InstallmentStatus.PAID


def test_payment_complete_with_unknown_installment_id_does_not_crash(client, db_session, order):
    resp = client.post(
        URL,
        json={
            "event": "PAYMENT_COMPLETE",
            "reference": f"order-{order.id}",
            "installment_id": "does-not-exist",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


def test_payment_failed_marks_order_failed(client, db_session, order):
    resp = client.post(URL, json={"event": "PAYMENT_FAILED", "reference": f"order-{order.id}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Order marked as FAILED"}
    db_session.refresh(order)
    assert order.status == OrderStatus.FAILED


def test_unhandled_event_type_is_ignored(client, order):
    resp = client.post(
        URL, json={"event": "SOME_OTHER_EVENT", "reference": f"order-{order.id}"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Unhandled event type"}
