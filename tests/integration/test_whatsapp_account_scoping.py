import pytest

from app.core.security import create_access_token
from app.db.models import User, WhatsAppAccount


@pytest.fixture
def owner_and_account(db_session):
    owner = User(username="255700000010")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)

    account = WhatsAppAccount(
        name="Owner's Account",
        phone_number_id="PHONE_OWNER",
        access_token="tok",
        user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    headers = {"Authorization": f"Bearer {create_access_token(owner.id)}"}
    return owner, account, headers


@pytest.fixture
def other_user_headers(db_session):
    other = User(username="255700000020")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def test_get_messages_requires_auth(client, owner_and_account):
    _, account, _ = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={"X-WhatsApp-Account-ID": str(account.id)},
    )

    assert resp.status_code == 403 or resp.status_code == 401


def test_get_messages_404_for_other_users_account_by_id(
    client, owner_and_account, other_user_headers
):
    _, account, _ = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={**other_user_headers, "X-WhatsApp-Account-ID": str(account.id)},
    )

    assert resp.status_code == 404


def test_get_messages_404_for_other_users_account_by_phone_number_id(
    client, owner_and_account, other_user_headers
):
    _, account, _ = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={
            **other_user_headers,
            "X-WhatsApp-Account-ID": account.phone_number_id,
        },
    )

    assert resp.status_code == 404


def test_get_messages_200_for_owner_by_id(client, owner_and_account):
    _, account, headers = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={**headers, "X-WhatsApp-Account-ID": str(account.id)},
    )

    assert resp.status_code == 200
    assert resp.json() == []


def test_get_messages_200_for_owner_by_phone_number_id(client, owner_and_account):
    _, account, headers = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={**headers, "X-WhatsApp-Account-ID": account.phone_number_id},
    )

    assert resp.status_code == 200


def test_get_messages_200_via_legacy_header(client, owner_and_account):
    _, account, headers = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={**headers, "X-Account-ID": str(account.id)},
    )

    assert resp.status_code == 200


def test_get_messages_404_for_nonexistent_account(client, owner_and_account):
    _, _, headers = owner_and_account

    resp = client.get(
        "/api/v1/whatsapp/messages",
        headers={**headers, "X-WhatsApp-Account-ID": "999999"},
    )

    assert resp.status_code == 404


def test_send_message_requires_auth(client, owner_and_account):
    _, account, _ = owner_and_account

    resp = client.post(
        "/api/v1/whatsapp/messages/send",
        headers={"X-WhatsApp-Account-ID": str(account.id)},
        json={"to": "255711111111", "type": "text", "text_body": "hi"},
    )

    assert resp.status_code in (401, 403)


def test_send_message_404_for_other_users_account(
    client, owner_and_account, other_user_headers
):
    _, account, _ = owner_and_account

    resp = client.post(
        "/api/v1/whatsapp/messages/send",
        headers={**other_user_headers, "X-WhatsApp-Account-ID": str(account.id)},
        json={"to": "255711111111", "type": "text", "text_body": "hi"},
    )

    assert resp.status_code == 404
