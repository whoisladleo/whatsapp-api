"""Operator trial grants (demo access): endpoints, access check, expiry."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import (
    Customer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    SupportAccessEvent,
    SupportAccessKind,
    User,
    WhatsAppAccount,
)
from app.services import subscription_access
from app.tasks import subscription_nudges as nudge_module

OPERATOR_PHONE = "255700000001"
TARGET_PHONE = "255700000009"


@pytest.fixture(autouse=True)
def _operators(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", OPERATOR_PHONE)
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", "")


@pytest.fixture
def operator(db_session):
    user = User(username=OPERATOR_PHONE)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def target(db_session):
    user = User(username=TARGET_PHONE)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def platform_product(db_session, operator):
    p = Product(
        slug="taskflow-os-subscription", name="Taskflow OS Subscription",
        max_price=100000, min_price=100000, quantity=999999,
        merchant_id=operator.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def target_channel(db_session, target):
    a = WhatsAppAccount(
        name="shop", phone_number_id="pn_trial", access_token="tok",
        ai_enabled=False, user_id=target.id,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _audit_rows(db_session, target):
    return (
        db_session.query(SupportAccessEvent)
        .filter(SupportAccessEvent.target_id == target.id)
        .all()
    )


# --- access check --------------------------------------------------------------


def test_trial_grants_access_without_subscription(
    db_session, platform_product, target
):
    target.trial_ends_at = datetime.utcnow() + timedelta(days=7)
    db_session.commit()

    assert subscription_access.is_merchant_allowed_ai(db_session, target) is True


def test_lapsed_trial_denies_access(db_session, platform_product, target):
    target.trial_ends_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    assert subscription_access.is_merchant_allowed_ai(db_session, target) is False


# --- grant ---------------------------------------------------------------------


def test_grant_by_days_flips_amara_on_immediately(
    client, db_session, operator, target, target_channel, platform_product
):
    resp = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "days": 14},
        headers=_headers(operator),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(target.id)
    assert body["amara_active"] is True
    ends_at = datetime.fromisoformat(body["trial_ends_at"])
    if ends_at.tzinfo is not None:
        ends_at = ends_at.replace(tzinfo=None)
    assert timedelta(days=13, hours=23) < ends_at - datetime.utcnow() < timedelta(days=14, hours=1)
    db_session.refresh(target_channel)
    assert target_channel.ai_enabled is True
    rows = _audit_rows(db_session, target)
    assert len(rows) == 1
    assert rows[0].kind == SupportAccessKind.TRIAL_GRANTED


def test_grant_by_absolute_date_and_regrant_changes_window(
    client, db_session, operator, target, platform_product
):
    first = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "days": 14},
        headers=_headers(operator),
    )
    assert first.status_code == 200

    later = (datetime.utcnow() + timedelta(days=60)).isoformat()
    second = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "ends_at": later},
        headers=_headers(operator),
    )

    assert second.status_code == 200
    assert second.json()["trial_ends_at"].startswith(later[:16])
    assert len(_audit_rows(db_session, target)) == 2


def test_grant_rejects_non_operator(client, db_session, target, platform_product):
    outsider = User(username="255700000077")
    db_session.add(outsider)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "days": 14},
        headers=_headers(outsider),
    )

    assert resp.status_code == 403


def test_grant_rejects_impersonated_session(client, db_session, operator, target):
    from app.services.support_access import IMPERSONATOR_CLAIM

    nested = create_access_token(target.id, extra_claims={IMPERSONATOR_CLAIM: str(operator.id)})
    resp = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "days": 14},
        headers={"Authorization": f"Bearer {nested}"},
    )

    assert resp.status_code == 403


def test_grant_validates_input(client, db_session, operator, target):
    headers = _headers(operator)
    # both shapes at once
    resp = client.post(
        "/api/v1/auth/trial",
        json={"user_id": str(target.id), "days": 7, "ends_at": datetime.utcnow().isoformat()},
        headers=headers,
    )
    assert resp.status_code == 422
    # neither shape
    resp = client.post(
        "/api/v1/auth/trial", json={"user_id": str(target.id)}, headers=headers
    )
    assert resp.status_code == 422
    # non-positive days
    resp = client.post(
        "/api/v1/auth/trial", json={"user_id": str(target.id), "days": 0}, headers=headers
    )
    assert resp.status_code == 422
    # past ends_at
    resp = client.post(
        "/api/v1/auth/trial",
        json={
            "user_id": str(target.id),
            "ends_at": (datetime.utcnow() - timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert resp.status_code == 400
    # unknown merchant
    resp = client.post(
        "/api/v1/auth/trial", json={"user_id": str(uuid.uuid4()), "days": 7}, headers=headers
    )
    assert resp.status_code == 404


# --- revoke --------------------------------------------------------------------


def test_revoke_clears_trial_and_flips_amara_off(
    client, db_session, operator, target, target_channel, platform_product
):
    target.trial_ends_at = datetime.utcnow() + timedelta(days=7)
    target_channel.ai_enabled = True
    db_session.commit()

    resp = client.delete(f"/api/v1/auth/trial/{target.id}", headers=_headers(operator))

    assert resp.status_code == 200
    assert resp.json()["trial_ends_at"] is None
    assert resp.json()["amara_active"] is False
    db_session.refresh(target_channel)
    assert target_channel.ai_enabled is False
    rows = _audit_rows(db_session, target)
    assert [r.kind for r in rows] == [SupportAccessKind.TRIAL_REVOKED]


def test_revoke_is_idempotent_and_paid_stays_on(
    client, db_session, operator, target, platform_product
):
    resp = client.delete(f"/api/v1/auth/trial/{target.id}", headers=_headers(operator))
    assert resp.status_code == 200
    assert resp.json()["amara_active"] is False

    # A paid merchant keeps Amara through a (no-op) revoke.
    customer = Customer(phone_number=target.username)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=platform_product.id,
        quantity=1, agreed_price=100000, payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=platform_product.id,
            monthly_amount=100000, status=ProductSubscriptionStatus.ACTIVE,
        )
    )
    db_session.commit()

    resp = client.delete(f"/api/v1/auth/trial/{target.id}", headers=_headers(operator))
    assert resp.status_code == 200
    assert resp.json()["amara_active"] is True


def test_revoke_rejects_non_operator(client, db_session, target):
    outsider = User(username="255700000077")
    db_session.add(outsider)
    db_session.commit()

    resp = client.delete(
        f"/api/v1/auth/trial/{target.id}", headers=_headers(outsider)
    )

    assert resp.status_code == 403


# --- expiry --------------------------------------------------------------------


def test_sweep_switches_off_lapsed_trial(
    db_session, platform_product, target, target_channel, monkeypatch
):
    target.trial_ends_at = datetime.utcnow() - timedelta(hours=1)
    target_channel.ai_enabled = True
    db_session.commit()
    target_id = target.id
    channel_id = target_channel.id
    monkeypatch.setattr(nudge_module, "SessionLocal", lambda: db_session)

    switched = nudge_module._switch_off_lapsed_trials(db_session)

    assert switched == 1
    row = db_session.query(WhatsAppAccount).filter_by(id=channel_id).one()
    assert row.ai_enabled is False
    user = db_session.query(User).filter_by(id=target_id).one()
    assert subscription_access.is_merchant_allowed_ai(db_session, user) is False


def test_sweep_leaves_active_trial_and_paid_alone(
    db_session, platform_product, target, target_channel, monkeypatch
):
    target.trial_ends_at = datetime.utcnow() + timedelta(days=7)
    target_channel.ai_enabled = True
    db_session.commit()
    monkeypatch.setattr(nudge_module, "SessionLocal", lambda: db_session)

    assert nudge_module._switch_off_lapsed_trials(db_session) == 0


# --- merchant list ---------------------------------------------------------------


def test_merchant_list_includes_trial_ends_at(
    client, db_session, operator, target, platform_product
):
    target.trial_ends_at = datetime.utcnow() + timedelta(days=3)
    db_session.commit()

    resp = client.get("/api/v1/auth/merchants", headers=_headers(operator))

    assert resp.status_code == 200
    row = next(m for m in resp.json()["items"] if m["id"] == str(target.id))
    assert row["trial_ends_at"] is not None
