import pytest

import app.tasks.number_billing as number_billing_tasks
from app.db.models import (
    NumberSubscription,
    NumberSubscriptionStatus,
    User,
    WhatsAppAccount,
)


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(number_billing_tasks, "SessionLocal", lambda: db_session)


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
        name="Billing Shop", phone_number_id="OLDPHONE", access_token="old-tok", user_id=owner.id
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def subscription(db_session, account):
    sub = NumberSubscription(
        account_id=account.id,
        status=NumberSubscriptionStatus.PROVISIONING,
        monthly_amount=10000,
        tunzaa_transaction_id="TXN-PAID",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _mock_apply_async(monkeypatch, target: str):
    calls = []

    def fake(*, args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(target, fake)
    return calls


def _mock_notify_merchant(monkeypatch):
    calls = []

    async def fake(db, account, body):
        calls.append(body)

    monkeypatch.setattr("app.services.number_billing.notify_merchant", fake)
    return calls


def test_provision_after_payment_success(db_session, subscription, account, monkeypatch):
    async def fake_provision(db, verified_name=None, country_code=None):
        return None

    def fake_reassign(db, acct):
        acct.phone_number_id = "NEWPHONE"
        acct.access_token = "new-tok"
        return acct

    async def fake_display_name(*a, **kw):
        return None

    async def fake_register(*a, **kw):
        return None

    monkeypatch.setattr("app.services.pool_provisioning.provision_pool_number", fake_provision)
    monkeypatch.setattr("app.crud.account.reassign_account_to_pool_number", fake_reassign)
    monkeypatch.setattr("app.crud.account.get_pool_two_step_pin", lambda db, account_id: "123456")
    monkeypatch.setattr("app.api.v1.accounts.request_display_name_change", fake_display_name)
    monkeypatch.setattr("app.api.v1.accounts.register_phone_number", fake_register)
    renewal_calls = _mock_apply_async(monkeypatch, "app.tasks.number_billing.charge_renewal.apply_async")
    notify_calls = _mock_notify_merchant(monkeypatch)

    number_billing_tasks.provision_after_payment(subscription.id)

    refreshed = db_session.query(NumberSubscription).filter(NumberSubscription.id == subscription.id).first()
    assert refreshed.status == NumberSubscriptionStatus.ACTIVE
    assert refreshed.next_billing_at is not None
    assert renewal_calls[0]["args"] == [str(subscription.id)]
    assert renewal_calls[0]["kwargs"]["countdown"] == 30 * 86400
    assert "ready" in notify_calls[0]


def test_provision_after_payment_failure_refunds(db_session, subscription, monkeypatch):
    async def fake_provision(db, verified_name=None, country_code=None):
        raise RuntimeError("Meta rejected it")

    refund_calls = []

    async def fake_refund(transaction_id, amount, reason):
        refund_calls.append((transaction_id, amount, reason))
        return {"status": "REFUNDED"}

    monkeypatch.setattr("app.services.pool_provisioning.provision_pool_number", fake_provision)
    monkeypatch.setattr("app.services.tunzaa.tunzaa_service.refund_payment", fake_refund)
    notify_calls = _mock_notify_merchant(monkeypatch)

    number_billing_tasks.provision_after_payment(subscription.id)

    refreshed = db_session.query(NumberSubscription).filter(NumberSubscription.id == subscription.id).first()
    assert refreshed.status == NumberSubscriptionStatus.PROVISIONING_FAILED
    assert refund_calls == [("TXN-PAID", 10000, "Number provisioning failed")]
    assert "refunded" in notify_calls[0]


def test_provision_after_payment_failure_refund_also_fails(db_session, subscription, monkeypatch):
    async def fake_provision(db, verified_name=None, country_code=None):
        raise RuntimeError("Meta rejected it")

    async def fake_refund_fails(transaction_id, amount, reason):
        raise RuntimeError("Tunzaa refund endpoint down")

    monkeypatch.setattr("app.services.pool_provisioning.provision_pool_number", fake_provision)
    monkeypatch.setattr("app.services.tunzaa.tunzaa_service.refund_payment", fake_refund_fails)
    notify_calls = _mock_notify_merchant(monkeypatch)

    number_billing_tasks.provision_after_payment(subscription.id)

    refreshed = db_session.query(NumberSubscription).filter(NumberSubscription.id == subscription.id).first()
    assert refreshed.status == NumberSubscriptionStatus.PROVISIONING_FAILED
    assert "contact support" in notify_calls[0]


def test_provision_after_payment_missing_subscription_does_not_crash(db_session):
    number_billing_tasks.provision_after_payment(999999)  # no exception


def test_charge_renewal_pushes_payment_when_active(db_session, account, monkeypatch):
    sub = NumberSubscription(account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000)
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = []

    async def fake_initiate(self, phone, amount, reference):
        calls.append((phone, amount, reference))
        return {}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate)

    number_billing_tasks.charge_renewal(sub.id)

    assert calls == [(account.owner.username, 10000, f"number-renewal-{sub.id}")]


def test_charge_renewal_skips_when_deactivated(db_session, account, monkeypatch):
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.DEACTIVATED, monthly_amount=10000
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    calls = []

    async def fake_initiate(self, phone, amount, reference):
        calls.append((phone, amount, reference))
        return {}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate)

    number_billing_tasks.charge_renewal(sub.id)

    assert calls == []


def test_charge_renewal_missing_subscription_does_not_crash(db_session):
    number_billing_tasks.charge_renewal(999999)  # no exception


def test_charge_renewal_logs_and_swallows_payment_push_failure(db_session, account, monkeypatch):
    sub = NumberSubscription(account_id=account.id, status=NumberSubscriptionStatus.ACTIVE, monthly_amount=10000)
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    async def fake_initiate_fails(self, phone, amount, reference):
        raise RuntimeError("Tunzaa is down")

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_initiate_fails)

    number_billing_tasks.charge_renewal(sub.id)  # no exception
