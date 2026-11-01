"""Support impersonation API tests (#75, max rigor).

Covers: mint matrix (operator kinds, attacker shapes, nesting, self,
operator-target, unknown), short-lived claims, revocation, expiry, money
blocks, tenant isolation, session shapes, and the audit ledger (START /
END / WRITE rows, GET exclusion, exit exclusion).
"""

import uuid
from datetime import datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import (
    InstagramAccount,
    NumberSubscription,
    NumberSubscriptionStatus,
    Product,
    SupportAccessEvent,
    SupportAccessKind,
    User,
    WhatsAppAccount,
)
from app.services.support_access import IMPERSONATOR_CLAIM

OPERATOR_PHONE = "255700000001"
TARGET_PHONE = "255700000009"


@pytest.fixture(autouse=True)
def _operators(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", OPERATOR_PHONE)
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", "@whoisladleo")


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
    db_session.add(
        Product(slug="tkettle", name="Target Kettle", max_price=50000.0,
                min_price=40000.0, quantity=2, merchant_id=user.id)
    )
    db_session.commit()
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _mint(client, operator_headers, phone=TARGET_PHONE):
    return client.post(
        "/api/v1/auth/impersonate", json={"target_phone": phone},
        headers=operator_headers,
    )


def _audit_rows(db_session, kind=None):
    q = db_session.query(SupportAccessEvent)
    if kind is not None:
        q = q.filter(SupportAccessEvent.kind == kind)
    return q.order_by(SupportAccessEvent.created_at).all()


# Mint matrix


def test_impersonation_token_is_non_refreshable(client, operator, target):
    # Impersonation stays short-lived with no refresh path: no bearer is
    # minted, and the JWT itself is useless at POST /auth/refresh.
    resp = _mint(client, _headers(operator))

    assert resp.status_code == 200
    assert resp.json()["refresh_token"] is None

    refresh = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": resp.json()["access_token"]},
    )
    assert refresh.status_code == 401


def test_operator_mints_for_merchant(client, db_session, operator, target):
    resp = _mint(client, _headers(operator))

    assert resp.status_code == 200
    payload = jwt.decode(
        resp.json()["access_token"], settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    assert payload["sub"] == str(target.id)
    assert payload[IMPERSONATOR_CLAIM] == str(operator.id)
    exp = datetime.utcfromtimestamp(payload["exp"])
    assert timedelta(minutes=25) < exp - datetime.utcnow() < timedelta(minutes=35)

    starts = _audit_rows(db_session, SupportAccessKind.START)
    assert len(starts) == 1
    assert starts[0].operator_id == operator.id
    assert starts[0].target_id == target.id
    assert starts[0].operator_label == OPERATOR_PHONE
    assert starts[0].target_label == TARGET_PHONE


def test_ig_operator_mints_without_any_phone(client, db_session, target):
    ig_user = User(username=None)
    db_session.add(ig_user)
    db_session.commit()
    db_session.refresh(ig_user)
    db_session.add(
        InstagramAccount(
            ig_business_id="ig_op_1", access_token="tok",
            username="WhoIsLadLeo", user_id=ig_user.id,
        )
    )
    db_session.commit()

    resp = _mint(client, _headers(ig_user))

    assert resp.status_code == 200
    starts = _audit_rows(db_session, SupportAccessKind.START)
    assert starts[0].operator_label == "@whoisladleo"


def test_non_operator_cannot_mint(client, db_session, target):
    stranger = User(username="255799988877")
    db_session.add(stranger)
    db_session.commit()

    resp = _mint(client, _headers(stranger))

    assert resp.status_code == 403
    assert _audit_rows(db_session) == []


def test_self_reported_phone_cannot_mint(client, db_session, target):
    """THE critical case: an IG-login attacker self-reports the operator's
    number via PATCH /me. phone_number must never grant operator power."""
    attacker = User(username=None, phone_number=OPERATOR_PHONE)
    db_session.add(attacker)
    db_session.commit()

    resp = _mint(client, _headers(attacker))

    assert resp.status_code == 403
    assert _audit_rows(db_session) == []


def test_empty_allow_list_mints_nothing(client, db_session, operator, target, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", None)
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", None)

    resp = _mint(client, _headers(operator))

    assert resp.status_code == 403


def test_nesting_blocked(client, db_session, operator, target):
    token = _mint(client, _headers(operator)).json()["access_token"]

    resp = client.post(
        "/api/v1/auth/impersonate", json={"target_phone": TARGET_PHONE},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert len(_audit_rows(db_session, SupportAccessKind.START)) == 1


def test_self_impersonation_rejected(client, db_session, operator, target):
    resp = client.post(
        "/api/v1/auth/impersonate", json={"target_phone": OPERATOR_PHONE},
        headers=_headers(operator),
    )

    assert resp.status_code == 400
    assert _audit_rows(db_session) == []


def test_second_operator_cannot_be_impersonated(
    client, db_session, operator, target, monkeypatch
):
    monkeypatch.setattr(
        settings, "SUPPORT_OPERATOR_PHONES", f"{OPERATOR_PHONE},255700000002"
    )
    second_op = User(username="255700000002")
    db_session.add(second_op)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/impersonate", json={"target_phone": "255700000002"},
        headers=_headers(operator),
    )

    assert resp.status_code == 400


def test_unknown_target_is_404(client, db_session, operator):
    resp = _mint(client, _headers(operator), phone="255700000099")

    assert resp.status_code == 404
    assert _audit_rows(db_session) == []


def test_mint_rate_limited(client, db_session, operator, target):
    headers = _headers(operator)
    for _ in range(20):
        assert _mint(client, headers).status_code == 200

    assert _mint(client, headers).status_code == 429


# Mint by ID (#76)


def _mint_by_id(client, operator_headers, user_id):
    return client.post(
        "/api/v1/auth/impersonate", json={"target_user_id": str(user_id)},
        headers=operator_headers,
    )


def test_mint_by_id_reaches_phoneless_merchant(client, db_session, operator):
    phoneless = User(username=None, phone_number=None, name="divinebundles_")
    db_session.add(phoneless)
    db_session.commit()

    resp = _mint_by_id(client, _headers(operator), phoneless.id)

    assert resp.status_code == 200
    payload = jwt.decode(
        resp.json()["access_token"], settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    assert payload["sub"] == str(phoneless.id)
    starts = _audit_rows(db_session, SupportAccessKind.START)
    assert len(starts) == 1
    assert starts[0].target_id == phoneless.id


def test_mint_by_unknown_id_is_404(client, db_session, operator):
    resp = _mint_by_id(client, _headers(operator), uuid.uuid4())

    assert resp.status_code == 404
    assert _audit_rows(db_session) == []


def test_mint_with_no_target_is_422(client, db_session, operator, target):
    assert _mint(client, _headers(operator), phone="   ").status_code == 422
    resp = client.post(
        "/api/v1/auth/impersonate", json={}, headers=_headers(operator)
    )
    assert resp.status_code == 422
    assert _audit_rows(db_session) == []


def test_mint_by_malformed_id_is_422(client, db_session, operator, target):
    resp = client.post(
        "/api/v1/auth/impersonate", json={"target_user_id": "not-a-uuid"},
        headers=_headers(operator),
    )

    assert resp.status_code == 422
    assert _audit_rows(db_session) == []


def test_mint_prefers_id_over_phone(client, db_session, operator, target):
    other = User(username="255700000007")
    db_session.add(other)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/impersonate",
        json={"target_user_id": str(target.id), "target_phone": "255700000007"},
        headers=_headers(operator),
    )

    assert resp.status_code == 200
    payload = jwt.decode(
        resp.json()["access_token"], settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    assert payload["sub"] == str(target.id)


def test_mint_by_id_self_and_operator_rejected(
    client, db_session, operator, target, monkeypatch
):
    assert _mint_by_id(client, _headers(operator), operator.id).status_code == 400

    monkeypatch.setattr(
        settings, "SUPPORT_OPERATOR_PHONES", f"{OPERATOR_PHONE},{TARGET_PHONE}"
    )
    assert _mint_by_id(client, _headers(operator), target.id).status_code == 400
    assert _audit_rows(db_session) == []


# Session shapes, revocation, expiry


def _session(client, headers):
    return client.get("/api/v1/auth/session", headers=headers)


def test_session_normal_merchant(client, db_session, target):
    resp = _session(client, _headers(target))

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == TARGET_PHONE
    assert body["is_operator"] is False
    assert body["impersonated_by"] is None


def test_session_operator_not_impersonating(client, db_session, operator):
    resp = _session(client, _headers(operator))

    assert resp.json()["is_operator"] is True
    assert resp.json()["impersonated_by"] is None


def test_session_impersonated_shows_target_and_mask(client, db_session, operator, target):
    token = _mint(client, _headers(operator)).json()["access_token"]

    resp = _session(client, {"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == TARGET_PHONE
    assert body["is_operator"] is True
    assert body["impersonated_by"]["id"] == str(operator.id)
    assert body["impersonated_by"]["label"] == OPERATOR_PHONE


def test_revoked_operator_killed_on_next_use(
    client, db_session, operator, target, monkeypatch
):
    token = _mint(client, _headers(operator)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", None)
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", None)

    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Operator access revoked"


def test_expired_impersonation_token_rejected(client, db_session, operator, target):
    expired = jwt.encode(
        {"sub": str(target.id), IMPERSONATOR_CLAIM: str(operator.id),
         "exp": datetime.utcnow() - timedelta(minutes=1)},
        settings.SECRET_KEY, algorithm=settings.ALGORITHM,
    )

    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert resp.status_code == 401


# Exit


def test_exit_records_end_and_normal_token_cannot_exit(
    client, db_session, operator, target
):
    token = _mint(client, _headers(operator)).json()["access_token"]

    resp = client.post(
        "/api/v1/auth/impersonate/exit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    ends = _audit_rows(db_session, SupportAccessKind.END)
    assert len(ends) == 1
    assert ends[0].operator_id == operator.id
    assert ends[0].target_id == target.id

    # The exit call itself must not also land a WRITE row.
    assert _audit_rows(db_session, SupportAccessKind.WRITE) == []

    resp = client.post(
        "/api/v1/auth/impersonate/exit", headers=_headers(operator)
    )
    assert resp.status_code == 400


# Tenant isolation + money blocks


def test_impersonated_session_sees_only_target_data(
    client, db_session, operator, target
):
    db_session.add(
        Product(slug="okettle", name="Operator Kettle", max_price=1.0,
                min_price=1.0, quantity=1, merchant_id=operator.id)
    )
    db_session.commit()
    token = _mint(client, _headers(operator)).json()["access_token"]

    resp = client.get(
        "/api/v1/products", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert names == ["Target Kettle"]


def test_impersonated_money_endpoints_forbidden(
    client, db_session, operator, target
):
    token = _mint(client, _headers(operator)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    pay = client.post("/api/v1/auth/me/subscription/pay", json={}, headers=headers)
    assert pay.status_code == 403

    buy = client.post(
        f"/api/v1/accounts/{uuid.uuid4()}/purchase-number", headers=headers
    )
    assert buy.status_code == 403


def test_operator_own_session_can_still_pay(client, db_session, operator, monkeypatch):
    """The money block keys off impersonation, not operator status — the
    operator's own session must behave like any merchant's."""
    from app.services import self_service_billing

    async def fake_pay_now(*a, **kw):
        return {"order_id": uuid.uuid4(), "message": "ok"}

    monkeypatch.setattr(self_service_billing, "pay_now", fake_pay_now)
    resp = client.post(
        "/api/v1/auth/me/subscription/pay", json={}, headers=_headers(operator)
    )

    assert resp.status_code == 200


# WRITE audit


def test_impersonated_write_audited_without_query_or_body(
    client, db_session, operator, target
):
    token = _mint(client, _headers(operator)).json()["access_token"]

    resp = client.post(
        "/api/v1/products?secret=should-not-log",
        json={"name": "Support Kettle", "max_price": 100.0, "min_price": 90.0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201

    writes = _audit_rows(db_session, SupportAccessKind.WRITE)
    assert len(writes) == 1
    row = writes[0]
    assert row.operator_id == operator.id
    assert row.target_id == target.id
    assert row.method == "POST"
    assert row.path == "/api/v1/products"
    assert "secret" not in (row.path + (row.detail or ""))
    assert "Support Kettle" not in (row.detail or "")


def test_gets_and_own_writes_not_audited(client, db_session, operator, target):
    token = _mint(client, _headers(operator)).json()["access_token"]
    imp = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/products", headers=imp).status_code == 200
    assert client.get("/api/v1/auth/session", headers=imp).status_code == 200
    assert client.post(
        "/api/v1/products",
        json={"name": "Own Kettle", "max_price": 1.0, "min_price": 1.0},
        headers=_headers(operator),
    ).status_code == 201

    assert _audit_rows(db_session, SupportAccessKind.WRITE) == []
    # Only the mint START exists.
    assert len(_audit_rows(db_session)) == 1


# Merchant list


def _list(client, headers, **params):
    return client.get("/api/v1/auth/merchants", headers=headers, params=params)


def test_operator_lists_impersonable_only(
    client, db_session, operator, target, monkeypatch
):
    monkeypatch.setattr(
        settings, "SUPPORT_OPERATOR_PHONES", f"{OPERATOR_PHONE},255700000002"
    )
    db_session.add(User(username="255700000002"))
    db_session.commit()

    resp = _list(client, _headers(operator))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [item["username"] for item in body["items"]] == [TARGET_PHONE]
    # No operator rows (self or others) — minting for either is a 400 anyway.
    assert OPERATOR_PHONE not in resp.text
    assert "255700000002" not in resp.text
    # Identity fields only — never OTP state or anything secret-shaped.
    # trial_ends_at is display state for the admin trial control, not a secret.
    assert set(body["items"][0]) == {
        "id", "username", "phone_number", "business_name", "name", "created_at",
        "trial_ends_at",
    }


def test_merchant_list_forbidden_to_all_but_own_operator_session(
    client, db_session, operator, target
):
    stranger = User(username="255799988877")
    db_session.add(stranger)
    db_session.commit()

    assert _list(client, _headers(stranger)).status_code == 403

    token = _mint(client, _headers(operator)).json()["access_token"]
    impersonated = _list(client, {"Authorization": f"Bearer {token}"})
    assert impersonated.status_code == 403


def test_merchant_list_search_filters(client, db_session, operator, target):
    target.business_name = "Target Kettles Ltd"
    db_session.add(User(username="255711122233", business_name="Other Shop"))
    db_session.commit()

    resp = _list(client, _headers(operator), q="kettles")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["username"] == TARGET_PHONE

    resp = _list(client, _headers(operator), q="255711122233")
    assert resp.json()["total"] == 1

    resp = _list(client, _headers(operator), q="no-such-merchant")
    assert resp.json() == {"items": [], "total": 0}


def test_merchant_list_paginates_against_full_total(
    client, db_session, operator, target
):
    for i in range(3):
        db_session.add(User(username=f"25572220000{i}"))
    db_session.commit()

    first = _list(client, _headers(operator), limit=2, offset=0).json()
    second = _list(client, _headers(operator), limit=2, offset=2).json()
    assert first["total"] == 4
    assert second["total"] == 4
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert len(set(ids)) == 4


def test_merchant_list_includes_phoneless_ig_merchant(
    client, db_session, operator, target
):
    ig_merchant = User(username=None, phone_number="255733344455")
    db_session.add(ig_merchant)
    db_session.commit()

    resp = _list(client, _headers(operator))

    assert resp.status_code == 200
    assert resp.json()["total"] == 2
    assert "255733344455" in resp.text


def _manual_refund_setup(db_session, target, refund_status="MANUAL"):
    account = WhatsAppAccount(
        name="Target Shop", phone_number_id="MASKPHONE", access_token="tok",
        user_id=target.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    sub = NumberSubscription(
        account_id=account.id, status=NumberSubscriptionStatus.PROVISIONING_FAILED,
        monthly_amount=10000, tunzaa_transaction_id="TXN-1", refund_status=refund_status,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return account, sub


def _mask_headers(client, operator):
    mint = _mint(client, _headers(operator))
    assert mint.status_code == 200
    return {"Authorization": f"Bearer {mint.json()['access_token']}"}


def test_mark_refunded_flips_manual_to_succeeded(client, db_session, operator, target):
    account, sub = _manual_refund_setup(db_session, target)

    resp = client.post(
        f"/api/v1/accounts/{account.id}/number-subscription/mark-refunded",
        headers=_mask_headers(client, operator),
    )

    assert resp.status_code == 200
    assert resp.json()["refund_status"] == "SUCCEEDED"
    writes = _audit_rows(db_session, SupportAccessKind.WRITE)
    assert any("mark-refunded" in (row.path or "") for row in writes)


def test_mark_refunded_forbidden_for_merchant_own_token(client, db_session, target):
    account, sub = _manual_refund_setup(db_session, target)

    resp = client.post(
        f"/api/v1/accounts/{account.id}/number-subscription/mark-refunded",
        headers=_headers(target),
    )

    assert resp.status_code == 403
    db_session.refresh(sub)
    assert sub.refund_status == "MANUAL"


def test_mark_refunded_400_when_nothing_owed(client, db_session, operator, target):
    account, sub = _manual_refund_setup(db_session, target, refund_status=None)

    resp = client.post(
        f"/api/v1/accounts/{account.id}/number-subscription/mark-refunded",
        headers=_mask_headers(client, operator),
    )

    assert resp.status_code == 400


def test_mark_refunded_idempotent_when_already_succeeded(client, db_session, operator, target):
    account, sub = _manual_refund_setup(db_session, target, refund_status="SUCCEEDED")

    resp = client.post(
        f"/api/v1/accounts/{account.id}/number-subscription/mark-refunded",
        headers=_mask_headers(client, operator),
    )

    assert resp.status_code == 200
    assert resp.json()["refund_status"] == "SUCCEEDED"
