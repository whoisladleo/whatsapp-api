import hashlib
import hmac
import json

import pytest

from app.core.config import settings
from app.db.models import (
    Customer,
    Installment,
    InstallmentStatus,
    Message,
    Order,
    OrderStatus,
    Product,
    User,
    WhatsAppAccount,
)

URL = "/api/v1/tunzaa/tunzaa-webhook"
SECRET = "test-tunzaa-secret"


@pytest.fixture(autouse=True)
def _tunzaa_secret(monkeypatch):
    """
    Pinned to a known value rather than whatever's ambient in .env, so
    signature computation in these tests is deterministic regardless of
    environment (the same reasoning as the WEBHOOK_VERIFY_TOKEN fix in the
    Meta webhook tests).
    """
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", SECRET)


def _sign(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True)
    return hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _post(client, payload, sign=True):
    headers = {"X-Signature": _sign(payload)} if sign else {}
    return client.post(URL, json=payload, headers=headers)


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255711112222")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def owner(db_session):
    u = User(username="255700000900")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def account(db_session, owner):
    a = WhatsAppAccount(
        name="Tunzaa Shop", phone_number_id="TUNZAA_WEBHOOK_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="tunzaa-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=2, merchant_id=owner.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def order(db_session, customer, account, product):
    o = Order(
        customer_id=customer.id, account_id=account.id, product_id=product.id,
        quantity=1, agreed_price=45000, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def _mock_send_text(monkeypatch, ok=True):
    async def fake(self, to, body, preview_url=False):
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.tunzaa"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake)


# --- payload / signature validation -----------------------------------------


def test_invalid_json_payload_returns_400(client):
    resp = client.post(URL, content=b"not-json", headers={"Content-Type": "application/json"})

    assert resp.status_code == 400


def test_missing_signature_rejected_when_secret_configured(client):
    resp = client.post(
        URL, json={"status": "COMPLETED", "reference_id": "order-1", "transaction_id": "T1"}
    )

    assert resp.status_code == 403


def test_wrong_signature_rejected(client):
    resp = client.post(
        URL,
        json={"status": "COMPLETED", "reference_id": "order-1", "transaction_id": "T1"},
        headers={"X-Signature": "0" * 64},
    )

    assert resp.status_code == 403


def test_verification_skipped_with_warning_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", None)

    resp = client.post(
        URL, json={"status": "COMPLETED", "reference_id": "order-999999", "transaction_id": "T1"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}


# --- reference/order resolution ---------------------------------------------


def test_missing_reference_is_ignored(client):
    resp = _post(client, {"status": "COMPLETED"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "No reference_id provided"}


def test_reference_without_dash_is_ignored(client, order):
    resp = _post(client, {"status": "COMPLETED", "reference_id": "noreference"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}


def test_reference_for_nonexistent_order_is_ignored(client):
    resp = _post(client, {"status": "COMPLETED", "reference_id": "order-999999"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}


# --- COMPLETED ---------------------------------------------------------------


def test_payment_completed_marks_order_paid_and_notifies_customer(client, db_session, order, monkeypatch):
    _mock_send_text(monkeypatch)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T1"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Order status: PAID"}
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert order.tunzaa_transaction_id == "T1"

    confirm = (
        db_session.query(Message)
        .filter(Message.status == "sent", Message.direction == "outbound")
        .first()
    )
    assert confirm is not None
    assert "confirmed" in confirm.body.lower()


def test_payment_completed_updates_matching_installment(client, db_session, order):
    installment = Installment(
        order_id=order.id, tunzaa_installment_id="inst-1", amount_due=15000,
        due_date=order.created_at, status=InstallmentStatus.PENDING,
    )
    db_session.add(installment)
    db_session.commit()
    db_session.refresh(installment)

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{order.id}", "installment_id": "inst-1"},
    )

    assert resp.status_code == 200
    db_session.refresh(installment)
    assert installment.status == InstallmentStatus.PAID


def test_payment_completed_with_unknown_installment_id_does_not_crash(client, db_session, order):
    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{order.id}", "installment_id": "does-not-exist"},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


# --- installment-plan reconciliation (order.tunzaa_plan_id set) -------------


def _mock_get_installment_plan(monkeypatch, response=None, raise_error=False):
    async def fake(self, plan_id, include_payments=False):
        if raise_error:
            raise RuntimeError("network down")
        return response

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.get_installment_plan", fake)


@pytest.fixture
def plan_order(db_session, order):
    order.tunzaa_plan_id = "1001"
    db_session.add(
        Installment(
            order_id=order.id, tunzaa_installment_id="10000", amount_due=15000,
            due_date=order.created_at, status=InstallmentStatus.PENDING,
        )
    )
    db_session.commit()
    db_session.refresh(order)
    return order


def test_installment_reconciliation_order_stays_pending_with_remaining_balance(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "30000.00"},
            "installments": [{"installment_id": 10000, "status": "PAID"}],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Order status: PENDING"}
    db_session.refresh(plan_order)
    assert plan_order.status == OrderStatus.PENDING

    installment = (
        db_session.query(Installment)
        .filter(Installment.order_id == plan_order.id, Installment.tunzaa_installment_id == "10000")
        .first()
    )
    assert installment.status == InstallmentStatus.PAID


def test_installment_reconciliation_marks_order_paid_when_balance_zero(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "0.00"},
            "installments": [{"installment_id": 10000, "status": "PAID"}],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    db_session.refresh(plan_order)
    assert plan_order.status == OrderStatus.PAID


def test_installment_reconciliation_marks_overdue_installment(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "15000.00"},
            "installments": [{"installment_id": 10000, "status": "OVERDUE"}],
        },
    )

    _post(client, {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"})

    installment = (
        db_session.query(Installment)
        .filter(Installment.order_id == plan_order.id, Installment.tunzaa_installment_id == "10000")
        .first()
    )
    assert installment.status == InstallmentStatus.OVERDUE


def test_installment_reconciliation_falls_back_when_get_plan_fails(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(monkeypatch, raise_error=True)

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    db_session.refresh(plan_order)
    assert plan_order.status == OrderStatus.PAID

    installment = (
        db_session.query(Installment)
        .filter(Installment.order_id == plan_order.id, Installment.tunzaa_installment_id == "10000")
        .first()
    )
    assert installment.status == InstallmentStatus.PAID


def test_installment_reconciliation_unparseable_remaining_balance_leaves_order_pending(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={"plan": {"plan_id": 1001, "remaining_balance": "not-a-number"}, "installments": []},
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    db_session.refresh(plan_order)
    assert plan_order.status == OrderStatus.PENDING


def test_installment_reconciliation_ignores_unmatched_installment_ids(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "30000.00"},
            "installments": [
                {"installment_id": None},
                {"installment_id": 99999, "status": "PAID"},
            ],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    installment = (
        db_session.query(Installment)
        .filter(Installment.order_id == plan_order.id, Installment.tunzaa_installment_id == "10000")
        .first()
    )
    assert installment.status == InstallmentStatus.PENDING


def test_payment_completed_notify_failure_still_logs_failed_message(client, db_session, order, monkeypatch):
    _mock_send_text(monkeypatch, ok=False)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T3"})

    assert resp.status_code == 200
    logged = db_session.query(Message).filter(Message.status == "failed").first()
    assert logged is not None


def test_payment_completed_without_account_does_not_crash(client, db_session, customer):
    o = Order(customer_id=customer.id, agreed_price=1000, status=OrderStatus.PENDING)
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{o.id}"})

    assert resp.status_code == 200
    db_session.refresh(o)
    assert o.status == OrderStatus.PAID


# --- FAILED / CANCELLED -------------------------------------------------------


def test_payment_failed_releases_stock_and_notifies(client, db_session, order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    stock_before = product.quantity

    resp = _post(client, {"status": "FAILED", "reference_id": f"order-{order.id}", "transaction_id": "T2"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Order marked as FAILED"}
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.FAILED
    assert product.quantity == stock_before + order.quantity

    notice = db_session.query(Message).filter(Message.status == "sent").first()
    assert notice is not None


def test_payment_cancelled_treated_same_as_failed(client, db_session, order):
    resp = _post(client, {"status": "CANCELLED", "reference_id": f"order-{order.id}"})

    assert resp.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.FAILED


def test_payment_failed_without_product_link_does_not_crash(client, db_session, customer):
    o = Order(customer_id=customer.id, agreed_price=1000, status=OrderStatus.PENDING)
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)

    resp = _post(client, {"status": "FAILED", "reference_id": f"order-{o.id}"})

    assert resp.status_code == 200
    db_session.refresh(o)
    assert o.status == OrderStatus.FAILED


# --- misc ---------------------------------------------------------------------


def test_unhandled_status_is_ignored(client, order):
    resp = _post(client, {"status": "SOME_OTHER_STATUS", "reference_id": f"order-{order.id}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Unhandled status"}


def test_payment_completed_with_dangling_account_id_does_not_crash(client, db_session, customer):
    o = Order(customer_id=customer.id, account_id=999999, agreed_price=1000, status=OrderStatus.PENDING)
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{o.id}"})

    assert resp.status_code == 200
    db_session.refresh(o)
    assert o.status == OrderStatus.PAID


def test_duplicate_transaction_id_ignored_after_processed(client, db_session, order):
    order.status = OrderStatus.PAID
    order.tunzaa_transaction_id = "T1"
    db_session.commit()

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T1"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Already processed"}
