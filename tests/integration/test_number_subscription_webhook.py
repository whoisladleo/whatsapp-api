import hashlib
import uuid
import hmac
import json
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    Message,
    NumberSubscription,
    NumberSubscriptionStatus,
    User,
    WhatsAppAccount,
)

URL = "/api/v1/tunzaa/tunzaa-webhook"
SECRET = "test-tunzaa-secret"


@pytest.fixture(autouse=True)
def _tunzaa_secret(monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", SECRET)


def _sign(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True)
    return hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _post(client, payload):
    return client.post(URL, json=payload, headers={"X-Signature": _sign(payload)})


def _mock_send_text(monkeypatch, ok=True):
    async def fake(self, to, body, preview_url=False):
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.fake"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake)


def _mock_apply_async(monkeypatch, target: str):
    calls = []

    def fake(*, args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(target, fake)
    return calls


@pytest.fixture
def account(db_session):
    user = User(username="255700000500")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    acct = WhatsAppAccount(
        name="Test Shop", phone_number_id="OLDPHONE1", access_token="old-token", user_id=user.id
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def subscription(db_session, account):
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.PENDING_PAYMENT, monthly_amount=10000
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def test_purchase_completed_moves_to_provisioning_and_dispatches_task(
    client, db_session, subscription, monkeypatch
):
    calls = _mock_apply_async(
        monkeypatch, "app.tasks.number_billing.provision_after_payment.apply_async"
    )

    resp = _post(
        client,
        {
            "status": "COMPLETED",
            "reference_id": f"number-purchase-{subscription.id}",
            "transaction_id": "TXN1",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(subscription)
    assert subscription.status == NumberSubscriptionStatus.PROVISIONING
    assert subscription.tunzaa_transaction_id == "TXN1"
    assert calls == [{"args": [str(subscription.id)], "kwargs": {}}]


def test_purchase_failed_marks_payment_failed_and_notifies(client, db_session, subscription, monkeypatch):
    _mock_send_text(monkeypatch)

    resp = _post(
        client, {"status": "FAILED", "reference_id": f"number-purchase-{subscription.id}", "transaction_id": "TXN2"}
    )

    assert resp.status_code == 200
    db_session.refresh(subscription)
    assert subscription.status == NumberSubscriptionStatus.PAYMENT_FAILED
    assert db_session.query(Message).filter(Message.status == "sent").count() == 1


def test_renewal_completed_activates_and_schedules_next_charge(client, db_session, account, monkeypatch):
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    resp = _post(
        client, {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN3"}
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.next_billing_at is not None
    assert calls[0]["args"] == [str(sub.id)]
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400


def test_renewal_failed_first_time_sets_past_due_and_retries(client, db_session, account, monkeypatch):
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000, retry_count=0
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    _mock_send_text(monkeypatch)
    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    resp = _post(
        client, {"status": "FAILED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN4"}
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.PAST_DUE
    assert sub.retry_count == 1
    assert calls[0]["kwargs"]["countdown"] == 24 * 60 * 60

    account_refreshed = db_session.query(WhatsAppAccount).filter(WhatsAppAccount.id == account.id).first()
    assert account_refreshed.ai_enabled is True  # not deactivated yet — only one failure


def test_renewal_failed_second_time_deactivates(client, db_session, account, monkeypatch):
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.PAST_DUE, monthly_amount=10000, retry_count=1
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    _mock_send_text(monkeypatch)

    resp = _post(
        client, {"status": "FAILED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN5"}
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.DEACTIVATED

    account_refreshed = db_session.query(WhatsAppAccount).filter(WhatsAppAccount.id == account.id).first()
    assert account_refreshed.ai_enabled is False


def test_duplicate_transaction_id_is_ignored(client, db_session, subscription, monkeypatch):
    calls = _mock_apply_async(
        monkeypatch, "app.tasks.number_billing.provision_after_payment.apply_async"
    )
    payload = {
        "status": "COMPLETED",
        "reference_id": f"number-purchase-{subscription.id}",
        "transaction_id": "TXN-DUP",
    }

    first = _post(client, payload)
    second = _post(client, payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["reason"] == "Already processed"
    assert len(calls) == 1  # not dispatched twice


def test_unknown_subscription_id_ignored(client):
    resp = _post(client, {"status": "COMPLETED", "reference_id": f"number-purchase-{uuid.uuid4()}"})

    assert resp.status_code == 200
    assert resp.json()["reason"] == "Subscription not found"


def test_malformed_reference_ignored(client):
    resp = _post(client, {"status": "COMPLETED", "reference_id": "number-purchase-not-a-number"})

    assert resp.status_code == 200
    assert resp.json()["reason"] == "Malformed reference_id"


@pytest.mark.parametrize("late_status", ["FAILED", "CANCELLED"])
@pytest.mark.parametrize(
    "resolved_status",
    [NumberSubscriptionStatus.PROVISIONING, NumberSubscriptionStatus.ACTIVE],
)
def test_purchase_late_failure_after_resolved_is_ignored(
    client, db_session, account, monkeypatch, late_status, resolved_status
):
    """P1: COMPLETED txn-A resolves the purchase; a late FAILED/CANCELLED
    with a *different* txn id passes the webhook's same-txn idempotency
    guard but must be ignored — never regress to PAYMENT_FAILED and never
    tell the merchant 'no number was purchased' (that false message would
    invite a repay double charge)."""
    sub = NumberSubscription(
        account_id=account.id,
        status=resolved_status,
        monthly_amount=10000,
        tunzaa_transaction_id="TXN-A",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    _mock_send_text(monkeypatch)

    resp = _post(
        client,
        {
            "status": late_status,
            "reference_id": f"number-purchase-{sub.id}",
            "transaction_id": "TXN-B",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == resolved_status
    assert sub.tunzaa_transaction_id == "TXN-A"
    assert db_session.query(Message).count() == 0


@pytest.mark.parametrize(
    "resolved_status",
    [NumberSubscriptionStatus.PROVISIONING, NumberSubscriptionStatus.ACTIVE],
)
def test_purchase_second_completed_with_new_txn_does_not_reprovision(
    client, db_session, account, monkeypatch, resolved_status
):
    """P2: a second COMPLETED with a new txn id is a genuine double payment,
    not a retry — it must NOT re-enter PROVISIONING, schedule a second
    provision_after_payment, or overwrite the first txn id. The duplicate
    cash is recorded (stats event + MANUAL refund-owed marker) and the
    merchant is told a double payment arrived."""
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.PENDING_PAYMENT, monthly_amount=10000
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = _mock_apply_async(
        monkeypatch, "app.tasks.number_billing.provision_after_payment.apply_async"
    )
    _mock_send_text(monkeypatch)

    first = _post(
        client,
        {
            "status": "COMPLETED",
            "reference_id": f"number-purchase-{sub.id}",
            "transaction_id": "TXN-A",
        },
    )
    assert first.status_code == 200

    if resolved_status == NumberSubscriptionStatus.ACTIVE:
        # Simulate the background provision task having finished.
        sub.status = NumberSubscriptionStatus.ACTIVE
        db_session.commit()

    second = _post(
        client,
        {
            "status": "COMPLETED",
            "reference_id": f"number-purchase-{sub.id}",
            "transaction_id": "TXN-B",
        },
    )

    assert second.status_code == 200
    db_session.refresh(sub)
    assert sub.status == resolved_status
    assert sub.tunzaa_transaction_id == "TXN-A"
    assert len(calls) == 1  # provisioned exactly once
    assert sub.refund_status == "MANUAL"

    events = (
        db_session.query(AmaraEvent)
        .filter(AmaraEvent.kind == AmaraEventKind.PAYMENT, AmaraEvent.account_id == account.id)
        .all()
    )
    assert len(events) == 1
    assert events[0].amount == 10000

    bodies = [m.body or "" for m in db_session.query(Message).all()]
    assert any("second payment" in b for b in bodies)


@pytest.mark.parametrize("late_status", ["FAILED", "CANCELLED"])
def test_renewal_late_failure_after_completed_is_ignored(
    client, db_session, account, monkeypatch, late_status
):
    """P3: COMPLETED txn-A pays the cycle (ACTIVE + one renewal task T+30d);
    a late FAILED/CANCELLED with a *different* txn id passes the webhook's
    same-txn guard but is stale — it must not regress to PAST_DUE nor
    schedule a 24h retry (that would charge the customer at 24h AND 30d)."""
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000, retry_count=0
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    _mock_send_text(monkeypatch)
    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    first = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-A"},
    )
    assert first.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert len(calls) == 1
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400
    paid_billing_at = sub.next_billing_at

    second = _post(
        client,
        {"status": late_status, "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-B"},
    )

    assert second.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.tunzaa_transaction_id == "TXN-A"
    assert sub.next_billing_at == paid_billing_at
    assert len(calls) == 1  # no 24h retry scheduled
    assert db_session.query(Message).count() == 0  # no false PAST_DUE notice


def test_renewal_duplicate_completed_schedules_only_one_task(
    client, db_session, account, monkeypatch
):
    """P4: a duplicate renewal COMPLETED with a *different* txn id passes the
    same-txn guard but is the same paid cycle — it must not reset
    next_billing_at nor schedule a second renewal task (two tasks would each
    push next cycle, double-charging)."""
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000, retry_count=0
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    first = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-A"},
    )
    assert first.status_code == 200
    db_session.refresh(sub)
    first_billing_at = sub.next_billing_at

    second = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-B"},
    )

    assert second.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.tunzaa_transaction_id == "TXN-A"
    assert sub.next_billing_at == first_billing_at
    assert len(calls) == 1


def test_renewal_failure_after_billing_date_still_retries(
    client, db_session, account, monkeypatch
):
    """P3 guardrail: the stale-FAILED ignore must not swallow a GENUINE
    next-cycle failure — once next_billing_at is due, FAILED still goes
    PAST_DUE with a 24h retry."""
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000, retry_count=0
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    _mock_send_text(monkeypatch)
    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    first = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-A"},
    )
    assert first.status_code == 200

    # A month passes: the scheduled charge fired and its push failed.
    sub.next_billing_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    second = _post(
        client,
        {"status": "FAILED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-C"},
    )

    assert second.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.PAST_DUE
    assert sub.retry_count == 1
    assert sub.tunzaa_transaction_id == "TXN-C"
    assert len(calls) == 2
    assert calls[1]["kwargs"]["countdown"] == 24 * 60 * 60
    assert db_session.query(Message).count() == 1


@pytest.mark.parametrize("stray_status", ["PENDING", None])
def test_renewal_stray_status_does_not_poison_idempotency(
    client, db_session, account, monkeypatch, stray_status
):
    """P5: a stray-status callback with txn-X must not record the txn id —
    otherwise the webhook's same-txn guard discards the genuine COMPLETED
    with txn-X as 'Already processed' and the paid cycle never activates."""
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.PAST_DUE, monthly_amount=10000, retry_count=0
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    stray_payload = {"reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-X"}
    if stray_status is not None:
        stray_payload["status"] = stray_status
    stray = _post(client, stray_payload)

    assert stray.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.PAST_DUE
    assert sub.tunzaa_transaction_id is None

    genuine = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-X"},
    )

    assert genuine.status_code == 200
    assert genuine.json().get("reason") != "Already processed"
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.tunzaa_transaction_id == "TXN-X"
    assert len(calls) == 1
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400


def test_renewal_completed_with_overdue_billing_date_still_renews(
    client, db_session, account, monkeypatch
):
    """P4 guardrail: the duplicate-COMPLETED ignore must not swallow a
    genuine late approval — ACTIVE with an overdue next_billing_at still
    renews (resets the billing date, schedules the next charge)."""
    sub = NumberSubscription(
        account_id=account.id,
        status=NumberSubscriptionStatus.ACTIVE,
        monthly_amount=10000,
        retry_count=0,
        next_billing_at=datetime.utcnow() - timedelta(days=2),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")

    resp = _post(
        client,
        {"status": "COMPLETED", "reference_id": f"number-renewal-{sub.id}", "transaction_id": "TXN-A"},
    )

    assert resp.status_code == 200
    db_session.refresh(sub)
    assert sub.status == NumberSubscriptionStatus.ACTIVE
    assert sub.retry_count == 0
    assert sub.next_billing_at > datetime.utcnow()
    assert len(calls) == 1
    assert calls[0]["kwargs"]["countdown"] == 30 * 86400
