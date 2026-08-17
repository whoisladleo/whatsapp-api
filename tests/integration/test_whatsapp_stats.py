from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.db.models import Message, User, WhatsAppAccount

URL = "/api/v1/whatsapp/stats"


@pytest.fixture
def owner_and_account(db_session):
    owner = User(username="255700001000")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)

    account = WhatsAppAccount(
        name="Stats Shop", phone_number_id="STATS_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    headers = {"Authorization": f"Bearer {create_access_token(owner.id)}", "X-WhatsApp-Account-ID": str(account.id)}
    return owner, account, headers


def _message(db_session, account, phone, direction, timestamp=None):
    kwargs = {
        "type": "text", "body": "hi", "direction": direction, "status": "sent",
        "account_id": account.id, "timestamp": timestamp or datetime.utcnow(),
    }
    if direction == "inbound":
        kwargs["from_phone"] = phone
    else:
        kwargs["to_phone"] = phone
    db_session.add(Message(**kwargs))
    db_session.commit()


def test_requires_auth(client, owner_and_account):
    _, account, _ = owner_and_account

    resp = client.get(URL, headers={"X-WhatsApp-Account-ID": str(account.id)})

    assert resp.status_code in (401, 403)


def test_zero_when_no_messages(client, owner_and_account):
    _, _, headers = owner_and_account

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"messages_sent": 0, "conversations": 0}


def test_counts_sent_messages_and_distinct_conversations(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    _message(db_session, account, "255722220001", "inbound")
    _message(db_session, account, "255722220001", "outbound")
    _message(db_session, account, "255722220001", "outbound")
    _message(db_session, account, "255722220002", "inbound")
    _message(db_session, account, "255722220002", "outbound")

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["messages_sent"] == 3
    assert body["conversations"] == 2


def test_excludes_merchants_own_number(client, db_session, owner_and_account):
    owner, account, headers = owner_and_account
    _message(db_session, account, owner.username, "inbound")
    _message(db_session, account, owner.username, "outbound")
    _message(db_session, account, "255722220003", "inbound")
    _message(db_session, account, "255722220003", "outbound")

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["messages_sent"] == 1
    assert body["conversations"] == 1
