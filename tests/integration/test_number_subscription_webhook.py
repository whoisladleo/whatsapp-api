import hashlib
import uuid
import hmac
import json

import pytest

from app.core.config import settings
from app.db.models import (
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
