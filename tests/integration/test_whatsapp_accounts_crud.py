import pytest

from app.core.security import create_access_token
from app.db.models import User, WhatsAppAccount

BASE = "/api/v1/whatsapp/accounts"


@pytest.fixture
def owner(db_session):
    user = User(username="255700000500")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def owner_headers(owner):
    return {"Authorization": f"Bearer {create_access_token(owner.id)}"}


@pytest.fixture
def other_headers(db_session):
    other = User(username="255700000501")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def _payload(**overrides):
    payload = {"name": "Shop", "phone_number_id": "WA_PHONE_1", "access_token": "tok"}
    payload.update(overrides)
    return payload


def test_create_account(client, owner_headers):
    resp = client.post(BASE, json=_payload(), headers=owner_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["phone_number_id"] == "WA_PHONE_1"


def test_create_account_duplicate_phone_number_id_rejected(client, owner_headers):
    client.post(BASE, json=_payload(), headers=owner_headers)

    resp = client.post(BASE, json=_payload(), headers=owner_headers)

    assert resp.status_code == 400


def test_list_accounts_scoped_to_user(client, owner_headers, other_headers):
    client.post(BASE, json=_payload(name="Mine"), headers=owner_headers)
    client.post(
        BASE, json=_payload(name="Theirs", phone_number_id="WA_PHONE_2"), headers=other_headers
    )

    resp = client.get(BASE, headers=owner_headers)

    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["Mine"]


def test_get_account_by_id(client, owner_headers):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.get(f"{BASE}/{created['id']}", headers=owner_headers)

    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_account_not_found_for_other_user(client, owner_headers, other_headers):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.get(f"{BASE}/{created['id']}", headers=other_headers)

    assert resp.status_code == 404


def test_update_account(client, owner_headers):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}", json={"ai_enabled": True}, headers=owner_headers
    )

    assert resp.status_code == 200
    assert resp.json()["ai_enabled"] is True


def test_update_account_not_found_for_other_user(client, owner_headers, other_headers):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.patch(
        f"{BASE}/{created['id']}", json={"ai_enabled": True}, headers=other_headers
    )

    assert resp.status_code == 404


def test_delete_account(client, owner_headers, db_session):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=owner_headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert db_session.query(WhatsAppAccount).filter(
        WhatsAppAccount.id == created["id"]
    ).first() is None


def test_delete_account_not_found_for_other_user(client, owner_headers, other_headers):
    created = client.post(BASE, json=_payload(), headers=owner_headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=other_headers)

    assert resp.status_code == 404


def test_messages_endpoint_missing_account_id_returns_400(client, owner_headers):
    resp = client.get("/api/v1/whatsapp/messages", headers=owner_headers)

    assert resp.status_code == 400
