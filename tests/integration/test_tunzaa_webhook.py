import hashlib
import hmac
import json
import uuid

import pytest

from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    Customer,
    Installment,
    InstallmentMode,
    InstallmentStatus,
    InstagramAccount,
    InstagramCustomer,
    InstagramDirectMessage,
    Message,
    Order,
    OrderStatus,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
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


def _mock_send_text(monkeypatch, ok=True, calls=None):
    async def fake(self, to, body, preview_url=False):
        if calls is not None:
            calls.append({"to": to, "body": body})
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


def test_unsigned_rejected_when_secret_unset_and_flag_off(client, monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", None)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)

    resp = client.post(
        URL, json={"status": "COMPLETED", "reference_id": "order-1", "transaction_id": "T1"}
    )

    assert resp.status_code == 403
    assert resp.json() == {"detail": "Webhook signature verification is not configured"}


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


def test_unknown_order_rejection_logs_claimed_reference_and_status(client, caplog):
    # A signed callback for an order we don't have must leave a trace with
    # the claimed reference — otherwise env mismatches and never-created
    # orders are invisible (#113).
    with caplog.at_level("WARNING", logger="app.api.v1.tunzaa_webhook"):
        resp = _post(
            client,
            {
                "status": "COMPLETED",
                "reference_id": "order-999999",
                "transaction_id": "TXN_GHOST",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Order not found"}
    assert "reference_id='order-999999'" in caplog.text
    assert "COMPLETED" in caplog.text
    assert "TXN_GHOST" in caplog.text


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


def test_installment_reconciliation_fetch_failure_leaves_order_pending_and_retries(
    client, db_session, plan_order, monkeypatch
):
    # Was test_installment_reconciliation_falls_back_when_get_plan_fails,
    # which asserted PAID here — that expectation was the #110 bug: a
    # mid-plan leg plus a transient Tunzaa fetch error marked the entire
    # order paid. The signed callback still proves the leg, so the leg is
    # recorded — but the order-level verdict waits for a successful fetch.
    _mock_get_installment_plan(monkeypatch, raise_error=True)

    scheduled = []
    from app.tasks.product_subscription_billing import retry_installment_reconcile
    monkeypatch.setattr(
        retry_installment_reconcile, "apply_async",
        lambda *a, **kw: scheduled.append((a, kw)),
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    db_session.refresh(plan_order)
    assert plan_order.status == OrderStatus.PENDING
    assert len(scheduled) == 1
    assert scheduled[0][1].get("countdown") is not None

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


def test_installment_retry_for_already_processed_leg_is_ignored(
    client, db_session, plan_order, monkeypatch
):
    """
    Regression guard: Tunzaa retries a callback up to 5 times per their
    docs. A non-final leg leaves order.status PENDING, so the order-level
    idempotency check alone doesn't catch a retry of that same leg — this
    would otherwise re-send the customer's payment-confirmation message on
    every retry. See #31.
    """
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "30000.00"},
            "installments": [{"installment_id": 10000, "status": "PAID"}],
        },
    )
    first = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )
    assert first.status_code == 200
    messages_after_first = db_session.query(Message).filter(Message.direction == "outbound").count()

    # Tunzaa retries the exact same callback for the same leg.
    retry = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert retry.status_code == 200
    assert retry.json() == {"status": "ignored", "reason": "Installment already processed"}
    messages_after_retry = db_session.query(Message).filter(Message.direction == "outbound").count()
    assert messages_after_retry == messages_after_first


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


# --- Instagram-originated orders ---------------------------------------------


@pytest.fixture
def ig_account(db_session, owner):
    a = InstagramAccount(
        ig_business_id="ig_biz_tunzaa_1", access_token="ig-tok", username="shop_ig", user_id=owner.id,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_tunzaa_1", username="buyer_ig", account_id=ig_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def ig_order(db_session, customer, ig_account, ig_customer, product):
    o = Order(
        customer_id=customer.id, product_id=product.id, instagram_account_id=ig_account.id,
        instagram_customer_id=ig_customer.id, quantity=1, agreed_price=45000, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def _mock_send_direct_message(monkeypatch, ok=True):
    async def fake(self, ig_business_id, recipient_id, message, tag=None):
        if ok:
            return {"recipient_id": recipient_id, "message_id": "ig_msg_notify_1"}
        return {"error": {"message": "rate limited"}}

    monkeypatch.setattr("app.core.instagram_provider.InstagramProvider.send_direct_message", fake)


def test_payment_completed_notifies_instagram_customer(client, db_session, ig_order, monkeypatch):
    """
    Regression test: _notify_customer used to only check order.account_id,
    which is always None for an Instagram-originated order — confirmed
    live, a real customer was never told their payment succeeded. See #31.
    """
    _mock_send_direct_message(monkeypatch)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{ig_order.id}", "transaction_id": "T-IG-1"})

    assert resp.status_code == 200
    db_session.refresh(ig_order)
    assert ig_order.status == OrderStatus.PAID

    confirm = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .first()
    )
    assert confirm is not None
    assert "confirmed" in confirm.body.lower()
    assert confirm.ig_message_id == "ig_msg_notify_1"


def test_payment_failed_notifies_instagram_customer(client, db_session, ig_order, monkeypatch):
    _mock_send_direct_message(monkeypatch)

    resp = _post(client, {"status": "FAILED", "reference_id": f"order-{ig_order.id}", "transaction_id": "T-IG-2"})

    assert resp.status_code == 200
    confirm = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .first()
    )
    assert confirm is not None
    assert "could not be completed" in confirm.body.lower()


def test_payment_completed_instagram_send_failure_does_not_crash(client, db_session, ig_order, monkeypatch):
    _mock_send_direct_message(monkeypatch, ok=False)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{ig_order.id}"})

    assert resp.status_code == 200
    assert (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .count()
        == 0
    )


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


def test_payment_failed_on_subscription_restores_no_stock(client, db_session, order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    product.is_subscription = True
    product.quantity = 0
    db_session.commit()

    resp = _post(client, {"status": "FAILED", "reference_id": f"order-{order.id}", "transaction_id": "T2"})

    assert resp.status_code == 200
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.FAILED
    assert product.quantity == 0


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


# --- out-of-order / new-transaction repeats (#77) -------------------------------


def _complete(client, order_id, txn, **extra):
    return _post(client, {
        "status": "COMPLETED", "reference_id": f"order-{order_id}",
        "transaction_id": txn, **extra,
    })


def _fail(client, order_id, txn):
    return _post(client, {
        "status": "FAILED", "reference_id": f"order-{order_id}",
        "transaction_id": txn,
    })


def test_failed_after_paid_does_not_regress(client, db_session, order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    _complete(client, order.id, "T-PAID-1")
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.PAID
    qty_paid = product.quantity

    resp = _fail(client, order.id, "T-ABANDONED-2")

    assert resp.json() == {"status": "ignored", "reason": "Order already resolved"}
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.PAID
    assert product.quantity == qty_paid
    assert order.tunzaa_transaction_id == "T-PAID-1"


def test_failed_twice_restores_stock_once(client, db_session, order, product):
    _fail(client, order.id, "T-F-1")
    db_session.refresh(product)
    assert product.quantity == 3  # 2 + 1 restored

    resp = _fail(client, order.id, "T-F-2")

    assert resp.json() == {"status": "ignored", "reason": "Order already resolved"}
    db_session.refresh(product)
    assert product.quantity == 3


def test_completed_after_paid_records_cash_and_marks_refund_without_customer_renotify(
    client, db_session, order, product, monkeypatch
):
    # P7: a genuine double payment (second COMPLETED, new txn) must alert
    # the merchant and flag the refund owed — previously only a stats event
    # was recorded and nobody was told. The customer already got their
    # confirmation on the first payment, so they are not re-notified.
    texts, templates = [], []
    _mock_send_text(monkeypatch, calls=texts)
    _mock_send_template(monkeypatch, calls=templates)
    _complete(client, order.id, "T-PAID-1")
    assert len(templates) == 1
    assert len(texts) == 1

    resp = _complete(client, order.id, "T-PAID-2", amount="45000")

    assert resp.json() == {"status": "success", "message": "Order already PAID; payment recorded"}
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert order.tunzaa_transaction_id == "T-PAID-1"
    assert order.refund_status == "MANUAL"
    assert len(texts) == 1
    assert len(templates) == 2
    _, summary, reference = _alert_values(templates[1])
    assert "duplicate" in summary.lower()
    assert "refund owed" in reference.lower()
    assert "T-PAID-2" in reference
    assert "Paid in full" not in reference
    payments = (
        db_session.query(AmaraEvent)
        .filter(AmaraEvent.kind == AmaraEventKind.PAYMENT)
        .all()
    )
    assert len(payments) == 2
    assert sum(p.amount or 0 for p in payments) == 90000.0


def test_failed_then_completed_reclaims_stock(client, db_session, order, product, monkeypatch):
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    _fail(client, order.id, "T-F-1")
    db_session.refresh(product)
    assert product.quantity == 3

    resp = _complete(client, order.id, "T-PAID-RETRY")

    assert resp.status_code == 200
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.PAID
    assert product.quantity == 2


def test_failed_then_completed_without_stock_stays_failed(
    client, db_session, order, product, monkeypatch
):
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    _fail(client, order.id, "T-F-1")
    product.quantity = 0  # sold out while the customer retried
    db_session.commit()

    resp = _complete(client, order.id, "T-PAID-RETRY")

    assert resp.json() == {"status": "success", "message": "Payment arrived but stock unavailable"}
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.FAILED
    assert product.quantity == 0
    assert order.tunzaa_transaction_id == "T-PAID-RETRY"


def test_cancelled_then_completed_reclaims_stock(client, db_session, order, product, monkeypatch):
    # A prompt approved after expiry (auto-cancel restores the reservation
    # just like FAILED does) must re-claim before marking paid (#110).
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    order.status = OrderStatus.CANCELLED
    product.quantity = 2
    db_session.commit()

    resp = _complete(client, order.id, "T-PAID-LATE")

    assert resp.status_code == 200
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.PAID
    assert product.quantity == 1


def test_cancelled_then_completed_without_stock_stays_cancelled(
    client, db_session, order, product, monkeypatch
):
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    order.status = OrderStatus.CANCELLED
    product.quantity = 0
    db_session.commit()

    resp = _complete(client, order.id, "T-PAID-LATE")

    assert resp.json() == {"status": "success", "message": "Payment arrived but stock unavailable"}
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.CANCELLED
    assert product.quantity == 0
    assert order.tunzaa_transaction_id == "T-PAID-LATE"


# --- paid-but-no-stock notifies both sides + marks refund owed (P6) ------------


def test_failed_then_completed_without_stock_notifies_and_marks_refund(
    client, db_session, order, product, monkeypatch
):
    texts, templates = [], []
    _mock_send_text(monkeypatch, calls=texts)
    _mock_send_template(monkeypatch, calls=templates)
    _fail(client, order.id, "T-F-1")
    product.quantity = 0  # sold out while the customer retried
    db_session.commit()

    resp = _complete(client, order.id, "T-PAID-RETRY")

    assert resp.json() == {"status": "success", "message": "Payment arrived but stock unavailable"}
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.FAILED
    assert product.quantity == 0
    assert order.tunzaa_transaction_id == "T-PAID-RETRY"
    # Refund-owed marker (MANUAL convention, cf. NumberSubscription).
    assert order.refund_status == "MANUAL"
    # Customer told (the earlier payment_failed text plus this notice) ...
    assert len(texts) == 2
    assert texts[0]["to"] == texts[1]["to"] == "255711112222"
    assert "refund" in texts[1]["body"].lower()
    assert "no longer available" in texts[1]["body"]
    # ... and the merchant alerted with shortage copy, not "Paid in full".
    assert len(templates) == 1
    assert templates[0]["to"] == "255700000900"
    _, summary, reference = _alert_values(templates[0])
    assert "out of stock" in summary
    assert "refund owed" in reference.lower()
    assert "Paid in full" not in reference
    # Real cash moved, so the payment is counted like the paid-twice branch.
    payments = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.PAYMENT).all()
    assert [(p.amount, p.product_name) for p in payments] == [(45000, "Shoes")]


def test_cancelled_then_completed_without_stock_notifies_and_marks_refund(
    client, db_session, order, product, monkeypatch
):
    texts, templates = [], []
    _mock_send_text(monkeypatch, calls=texts)
    _mock_send_template(monkeypatch, calls=templates)
    order.status = OrderStatus.CANCELLED
    product.quantity = 0
    db_session.commit()

    resp = _complete(client, order.id, "T-PAID-LATE")

    assert resp.json() == {"status": "success", "message": "Payment arrived but stock unavailable"}
    db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.refund_status == "MANUAL"
    assert len(texts) == 1
    assert texts[0]["to"] == "255711112222"
    assert "refund" in texts[0]["body"].lower()
    assert len(templates) == 1
    _, summary, reference = _alert_values(templates[0])
    assert "out of stock" in summary
    assert "refund owed" in reference.lower()


# --- leg-aware FAILED / CANCELLED (P8) ---------------------------------------

# NOTE: whether Tunzaa actually sends leg-level FAILED/CANCELLED callbacks
# with installment_id is UNVERIFIED — these tests pin that order-level
# callbacks (no installment_id, or no legs paid) behave exactly as before,
# and that a leg-level failure with earlier legs PAID fails only the leg.


def _add_leg(db_session, order, tunzaa_id, status=InstallmentStatus.PENDING):
    leg = Installment(
        order_id=order.id, tunzaa_installment_id=tunzaa_id, amount_due=15000,
        due_date=order.created_at, status=status,
    )
    db_session.add(leg)
    db_session.commit()
    db_session.refresh(leg)
    return leg


def _leg(db_session, order, tunzaa_id):
    return (
        db_session.query(Installment)
        .filter(
            Installment.order_id == order.id,
            Installment.tunzaa_installment_id == tunzaa_id,
        )
        .one()
    )


@pytest.mark.parametrize("status_value", ["FAILED", "CANCELLED"])
def test_leg_failure_with_paid_legs_keeps_order_pending_and_holds_stock(
    client, db_session, plan_order, product, monkeypatch, status_value
):
    texts = []
    _mock_send_text(monkeypatch, calls=texts)
    _leg(db_session, plan_order, "10000").status = InstallmentStatus.PAID
    _add_leg(db_session, plan_order, "10001")
    db_session.refresh(product)
    stock_before = product.quantity

    resp = _post(
        client,
        {
            "status": status_value, "reference_id": f"order-{plan_order.id}",
            "installment_id": "10001", "transaction_id": "T-LEG-FAIL",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Installment payment failed; order remains PENDING"}
    db_session.refresh(plan_order)
    db_session.refresh(product)
    assert plan_order.status == OrderStatus.PENDING
    assert product.quantity == stock_before
    assert _leg(db_session, plan_order, "10001").status == InstallmentStatus.OVERDUE
    assert _leg(db_session, plan_order, "10000").status == InstallmentStatus.PAID
    # The order wasn't cancelled, so no cancellation notice goes out.
    assert texts == []


def test_leg_failure_with_paid_legs_keeps_subscription_pending(
    client, db_session, snpl_subscription_order, snpl_pending_subscription, monkeypatch
):
    _mock_send_text(monkeypatch)
    _leg(db_session, snpl_subscription_order, "20000").status = InstallmentStatus.PAID
    db_session.add(
        Installment(
            order_id=snpl_subscription_order.id, tunzaa_installment_id="20001", amount_due=50000,
            due_date=snpl_subscription_order.created_at, status=InstallmentStatus.PENDING,
        )
    )
    db_session.commit()

    resp = _post(
        client,
        {
            "status": "FAILED", "reference_id": f"order-{snpl_subscription_order.id}",
            "installment_id": "20001", "transaction_id": "T-LEG-FAIL-SNPL",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(snpl_subscription_order)
    db_session.refresh(snpl_pending_subscription)
    assert snpl_subscription_order.status == OrderStatus.PENDING
    assert snpl_pending_subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


def test_leg_failure_with_no_legs_paid_fails_whole_order(
    client, db_session, plan_order, product, monkeypatch
):
    _mock_send_text(monkeypatch)
    stock_before = product.quantity

    resp = _post(
        client,
        {
            "status": "FAILED", "reference_id": f"order-{plan_order.id}",
            "installment_id": "10000", "transaction_id": "T-LEG-FAIL-1",
        },
    )

    assert resp.json() == {"status": "success", "message": "Order marked as FAILED"}
    db_session.refresh(plan_order)
    db_session.refresh(product)
    assert plan_order.status == OrderStatus.FAILED
    assert product.quantity == stock_before + plan_order.quantity


def test_order_level_failure_on_plan_with_paid_legs_still_fails_fast(
    client, db_session, plan_order, product, monkeypatch
):
    _mock_send_text(monkeypatch)
    _leg(db_session, plan_order, "10000").status = InstallmentStatus.PAID
    db_session.commit()
    db_session.refresh(product)
    stock_before = product.quantity

    resp = _post(
        client,
        {"status": "FAILED", "reference_id": f"order-{plan_order.id}", "transaction_id": "T-ORD-FAIL"},
    )

    assert resp.json() == {"status": "success", "message": "Order marked as FAILED"}
    db_session.refresh(plan_order)
    db_session.refresh(product)
    assert plan_order.status == OrderStatus.FAILED
    assert product.quantity == stock_before + plan_order.quantity


# --- misc ---------------------------------------------------------------------


def test_unhandled_status_is_ignored(client, order):
    resp = _post(client, {"status": "SOME_OTHER_STATUS", "reference_id": f"order-{order.id}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Unhandled status"}


def test_payment_completed_with_dangling_account_id_does_not_crash(client, db_session, customer):
    o = Order(customer_id=customer.id, account_id=uuid.uuid4(), agreed_price=1000, status=OrderStatus.PENDING)
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


# --- ProductSubscription: first payment (order-<id> reference) --------------


@pytest.fixture
def subscription_product(db_session, owner):
    p = Product(
        slug="tunzaa-sub", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def subscription_order(db_session, customer, account, subscription_product):
    o = Order(
        customer_id=customer.id, account_id=account.id, product_id=subscription_product.id,
        quantity=1, agreed_price=100000, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture
def pending_subscription(db_session, subscription_order, customer):
    sub = ProductSubscription(
        order_id=subscription_order.id, customer_id=customer.id, product_id=subscription_order.product_id,
        monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_first_payment_completed_activates_linked_subscription(
    client, db_session, subscription_order, pending_subscription, monkeypatch
):
    _mock_send_text(monkeypatch)

    resp = _post(
        client, {"status": "COMPLETED", "reference_id": f"order-{subscription_order.id}", "transaction_id": "T-SUB-1"}
    )

    assert resp.status_code == 200
    db_session.refresh(pending_subscription)
    assert pending_subscription.status == ProductSubscriptionStatus.ACTIVE
    assert pending_subscription.next_billing_at is not None


# --- ProductSubscription: BNPL vs SNPL installment activation ---------------


@pytest.fixture
def snpl_subscription_product(db_session, owner):
    p = Product(
        slug="tunzaa-sub-snpl", name="Taskflow OS Subscription (SNPL)", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=owner.id, is_subscription=True, installment_mode=InstallmentMode.SNPL,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def snpl_subscription_order(db_session, customer, account, snpl_subscription_product):
    o = Order(
        customer_id=customer.id, account_id=account.id, product_id=snpl_subscription_product.id,
        quantity=1, agreed_price=100000, status=OrderStatus.PENDING, tunzaa_plan_id="2001",
    )
    db_session.add(o)
    db_session.commit()
    db_session.add(
        Installment(
            order_id=o.id, tunzaa_installment_id="20000", amount_due=50000,
            due_date=o.created_at, status=InstallmentStatus.PENDING,
        )
    )
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture
def snpl_pending_subscription(db_session, snpl_subscription_order, customer):
    sub = ProductSubscription(
        order_id=snpl_subscription_order.id, customer_id=customer.id,
        product_id=snpl_subscription_order.product_id, monthly_amount=100000,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_snpl_first_installment_does_not_activate_subscription(
    client, db_session, snpl_subscription_order, snpl_pending_subscription, monkeypatch
):
    # Partial payment — plan still has a balance outstanding.
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 2001, "remaining_balance": "50000.00"},
            "installments": [{"installment_id": 20000, "status": "PAID"}],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{snpl_subscription_order.id}", "installment_id": "20000"},
    )

    assert resp.status_code == 200
    db_session.refresh(snpl_subscription_order)
    assert snpl_subscription_order.status == OrderStatus.PENDING
    db_session.refresh(snpl_pending_subscription)
    assert snpl_pending_subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


def test_snpl_final_installment_activates_subscription(
    client, db_session, snpl_subscription_order, snpl_pending_subscription, monkeypatch
):
    # Plan fully paid off — remaining_balance reaches zero.
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 2001, "remaining_balance": "0.00"},
            "installments": [{"installment_id": 20000, "status": "PAID"}],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{snpl_subscription_order.id}", "installment_id": "20000"},
    )

    assert resp.status_code == 200
    db_session.refresh(snpl_subscription_order)
    assert snpl_subscription_order.status == OrderStatus.PAID
    db_session.refresh(snpl_pending_subscription)
    assert snpl_pending_subscription.status == ProductSubscriptionStatus.ACTIVE


def test_bnpl_first_installment_activates_subscription_immediately(
    client, db_session, subscription_order, pending_subscription, monkeypatch
):
    # subscription_product defaults to BNPL — a partial installment payment
    # (order stays PENDING) should still activate right away, same as
    # pre-existing behavior.
    subscription_order.tunzaa_plan_id = "3001"
    db_session.add(
        Installment(
            order_id=subscription_order.id, tunzaa_installment_id="30000", amount_due=50000,
            due_date=subscription_order.created_at, status=InstallmentStatus.PENDING,
        )
    )
    db_session.commit()

    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 3001, "remaining_balance": "50000.00"},
            "installments": [{"installment_id": 30000, "status": "PAID"}],
        },
    )

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{subscription_order.id}", "installment_id": "30000"},
    )

    assert resp.status_code == 200
    db_session.refresh(subscription_order)
    assert subscription_order.status == OrderStatus.PENDING
    db_session.refresh(pending_subscription)
    assert pending_subscription.status == ProductSubscriptionStatus.ACTIVE


def test_first_payment_failed_marks_subscription_payment_failed(
    client, db_session, subscription_order, pending_subscription, monkeypatch
):
    _mock_send_text(monkeypatch)

    resp = _post(client, {"status": "FAILED", "reference_id": f"order-{subscription_order.id}"})

    assert resp.status_code == 200
    db_session.refresh(pending_subscription)
    assert pending_subscription.status == ProductSubscriptionStatus.PAYMENT_FAILED


def test_completed_order_with_no_linked_subscription_is_unaffected(client, db_session, order, monkeypatch):
    """A regular (non-subscription) order's COMPLETED callback shouldn't
    error out just because _activate_subscription_if_linked runs for every
    order now."""
    _mock_send_text(monkeypatch)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T-REG-1"})

    assert resp.status_code == 200
    assert db_session.query(ProductSubscription).count() == 0


# --- ProductSubscription: renewals (product-subscription-renewal-<id>) ------


def test_renewal_reference_for_unknown_subscription_is_ignored(client):
    resp = _post(client, {"status": "COMPLETED", "reference_id": f"product-subscription-renewal-{uuid.uuid4()}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Subscription not found"}


def test_renewal_completed_reactivates_subscription(client, db_session, subscription_order, customer):
    sub = ProductSubscription(
        order_id=subscription_order.id, customer_id=customer.id, product_id=subscription_order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000, retry_count=2,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"product-subscription-renewal-{sub.id}", "transaction_id": "T-REN-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    db_session.refresh(sub)
    assert sub.status == ProductSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.tunzaa_transaction_id == "T-REN-1"


def test_renewal_failed_moves_to_past_due_and_notifies_customer(
    client, db_session, subscription_order, customer, monkeypatch
):
    _mock_send_text(monkeypatch)
    sub = ProductSubscription(
        order_id=subscription_order.id, customer_id=customer.id, product_id=subscription_order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000, retry_count=0,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    resp = _post(
        client, {"status": "FAILED", "reference_id": f"product-subscription-renewal-{sub.id}", "transaction_id": "T-REN-2"}
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == ProductSubscriptionStatus.PAST_DUE
    assert sub.retry_count == 1


def test_renewal_failed_cancels_after_max_retries(
    client, db_session, subscription_order, customer, monkeypatch
):
    from app.services import product_subscription as product_subscription_service

    _mock_send_text(monkeypatch)
    sub = ProductSubscription(
        order_id=subscription_order.id, customer_id=customer.id, product_id=subscription_order.product_id,
        status=ProductSubscriptionStatus.PAST_DUE, monthly_amount=100000,
        retry_count=product_subscription_service.MAX_RENEWAL_RETRIES,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    resp = _post(
        client, {"status": "FAILED", "reference_id": f"product-subscription-renewal-{sub.id}", "transaction_id": "T-REN-3"}
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == ProductSubscriptionStatus.CANCELLED


def test_renewal_duplicate_transaction_id_ignored(client, db_session, subscription_order, customer):
    sub = ProductSubscription(
        order_id=subscription_order.id, customer_id=customer.id, product_id=subscription_order.product_id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000, tunzaa_transaction_id="T-REN-4",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    resp = _post(
        client, {"status": "COMPLETED", "reference_id": f"product-subscription-renewal-{sub.id}", "transaction_id": "T-REN-4"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Already processed"}


def test_renewal_malformed_reference_is_ignored(client):
    resp = _post(client, {"status": "COMPLETED", "reference_id": "product-subscription-renewal-not-a-number"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "Malformed reference_id"}


# --- merchant payment alert (#35) ---------------------------------------------


def _mock_send_template(monkeypatch, ok=True, calls=None):
    async def fake(self, to, name, language_code="en_US", components=None):
        if calls is not None:
            calls.append({"to": to, "name": name, "language_code": language_code, "components": components})
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.payment_alert"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", fake)


def _alert_values(call):
    """Pulls {{product}}, {{summary}}, {{reference}} back out of a captured send_template call."""
    header_params = call["components"][0]["parameters"]
    body_params = call["components"][1]["parameters"]
    by_name = {p["parameter_name"]: p["text"] for p in header_params + body_params}
    return by_name["product"], by_name["summary"], by_name["reference"]


def test_full_payment_sends_merchant_alert(client, db_session, order, monkeypatch):
    _mock_send_text(monkeypatch)
    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T-ALERT-1"})

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["to"] == "255700000900"  # owner.username
    assert calls[0]["name"] == settings.WHATSAPP_PAYMENT_ALERT_TEMPLATE_NAME
    product, summary, reference = _alert_values(calls[0])
    assert product == "Shoes"
    assert "full payment" in summary
    assert "45,000 TZS" in summary or f"{order.agreed_price:,.0f} TZS" in summary
    assert "255711112222" in reference
    assert "WhatsApp" in reference
    assert "Paid in full" in reference
    assert "T-ALERT-1" in reference


def test_installment_leg_with_remaining_balance_sends_merchant_alert(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "paid_amount": "15000.00", "remaining_balance": "15000.00"},
            "installments": [{"installment_id": 10000, "status": "PAID"}],
        },
    )
    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    resp = _post(
        client,
        {
            "status": "COMPLETED", "reference_id": f"order-{plan_order.id}",
            "installment_id": "10000", "transaction_id": "T-LEG-1",
        },
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    _, summary, reference = _alert_values(calls[0])
    assert "installment payment" in summary
    assert "final" not in summary
    assert "Total paid: 15,000 TZS" in reference
    assert "Remaining: 15,000 TZS" in reference
    assert "T-LEG-1" in reference


def test_final_installment_leg_sends_merchant_alert_as_fully_paid(
    client, db_session, plan_order, monkeypatch
):
    _mock_get_installment_plan(
        monkeypatch,
        response={
            "plan": {"plan_id": 1001, "remaining_balance": "0.00"},
            "installments": [{"installment_id": 10000, "status": "PAID"}],
        },
    )
    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"order-{plan_order.id}", "installment_id": "10000"},
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    _, summary, reference = _alert_values(calls[0])
    assert "final installment" in summary
    assert "fully paid" in summary


def test_instagram_order_merchant_alert_uses_instagram_handle_and_channel(
    client, db_session, ig_order, monkeypatch
):
    _mock_send_direct_message(monkeypatch)
    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{ig_order.id}", "transaction_id": "T-ALERT-IG"})

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["to"] == "255700000900"  # owner.username — merchant's own number, not the customer's
    _, _, reference = _alert_values(calls[0])
    assert "@buyer_ig" in reference
    assert "Instagram" in reference


def test_merchant_with_no_phone_on_file_skips_payment_alert(client, db_session, customer, product, monkeypatch):
    phoneless_owner = User(username=None)
    db_session.add(phoneless_owner)
    db_session.commit()
    db_session.refresh(phoneless_owner)

    account = WhatsAppAccount(
        name="No Phone Shop", phone_number_id="NO_PHONE_PHONE", access_token="tok", user_id=phoneless_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    o = Order(
        customer_id=customer.id, account_id=account.id, product_id=product.id,
        quantity=1, agreed_price=45000, status=OrderStatus.PENDING,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)

    _mock_send_text(monkeypatch)
    calls = []
    _mock_send_template(monkeypatch, calls=calls)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{o.id}", "transaction_id": "T-NO-PHONE"})

    assert resp.status_code == 200
    assert calls == []


def test_merchant_alert_send_failure_does_not_crash(client, db_session, order, monkeypatch):
    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch, ok=False)

    resp = _post(client, {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T-ALERT-FAIL"})

    assert resp.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


# --- Usage ledger (#41) -----------------------------------------------------------


def test_full_payment_records_payment_and_metered_buyer_notice(client, db_session, order, owner, monkeypatch):
    from app.db.models import AmaraEvent, AmaraEventKind

    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)
    payload = {"status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T-LEDGER-1"}

    _post(client, payload)
    _post(client, payload)  # Tunzaa retry: must not double-count

    payments = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.PAYMENT).all()
    assert [(p.merchant_id, p.amount, p.product_name) for p in payments] == [(owner.id, 45000, "Shoes")]
    # The buyer's "payment received" text is an automated send, so billable;
    # the merchant's payment_received alert is not.
    replies = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).all()
    assert [(r.merchant_id, r.channel) for r in replies] == [(owner.id, "whatsapp")]


def test_payment_event_uses_tunzaa_amount_when_given(client, db_session, order, monkeypatch):
    from app.db.models import AmaraEvent, AmaraEventKind

    _mock_send_text(monkeypatch)
    _mock_send_template(monkeypatch)

    _post(client, {
        "status": "COMPLETED", "reference_id": f"order-{order.id}", "transaction_id": "T-LEDGER-2", "amount": "44000",
    })

    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.PAYMENT).one().amount == 44000


# --- atomic stock release (P10) / single-transaction reclaim (P11) -----------------


def test_failed_loses_race_restores_no_stock(client, db_session, order, product, monkeypatch):
    # P10: auto-cancel (or a dashboard cancel) resolved the order after this
    # callback read it — the callback must not restore a second unit.
    _mock_send_text(monkeypatch)
    db_session.query(Order).filter(Order.id == order.id).update(
        {Order.status: OrderStatus.CANCELLED}, synchronize_session=False
    )
    assert order.status == OrderStatus.PENDING  # stale, as a race loser sees it

    resp = _fail(client, order.id, "T-F-RACE")

    assert resp.json() == {"status": "ignored", "reason": "Order already resolved"}
    db_session.refresh(product)
    assert product.quantity == 2


def test_try_reclaim_stock_defers_commit_to_caller(db_session, order, product):
    # P11: the reclaim must not commit internally — a crash (or a concurrent
    # COMPLETED) between its commit and the caller's loses a unit. Rolling
    # back after the call alone must undo the re-decrement.
    from app.api.v1.tunzaa_webhook import _try_reclaim_stock

    assert _try_reclaim_stock(db_session, order) is True

    db_session.rollback()
    db_session.refresh(product)
    assert product.quantity == 2
