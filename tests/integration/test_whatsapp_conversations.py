from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.db.models import ConversationContext, ConversationState, Customer, Message, User, WhatsAppAccount

URL = "/api/v1/whatsapp/conversations"


@pytest.fixture
def owner_and_account(db_session):
    owner = User(username="255700000900")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)

    account = WhatsAppAccount(
        name="Conversations Shop", phone_number_id="CONV_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    headers = {"Authorization": f"Bearer {create_access_token(owner.id)}", "X-WhatsApp-Account-ID": str(account.id)}
    return owner, account, headers


def _message(db_session, account, phone, body, direction, timestamp):
    kwargs = {"type": "text", "body": body, "direction": direction, "status": "sent", "account_id": account.id, "timestamp": timestamp}
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


def test_empty_when_no_messages(client, owner_and_account):
    _, _, headers = owner_and_account

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_orders_by_most_recent_activity(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    now = datetime.utcnow()
    _message(db_session, account, "255722220001", "old customer", "inbound", now - timedelta(hours=2))
    _message(db_session, account, "255722220002", "newest customer", "inbound", now)
    _message(db_session, account, "255722220003", "middle customer", "inbound", now - timedelta(hours=1))

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    phones = [c["customer_phone"] for c in resp.json()]
    assert phones == ["255722220002", "255722220003", "255722220001"]


def test_includes_last_message_and_state(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    now = datetime.utcnow()
    customer = Customer(phone_number="255722220004")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF))
    db_session.commit()

    _message(db_session, account, "255722220004", "first message", "inbound", now - timedelta(minutes=5))
    _message(db_session, account, "255722220004", "latest reply", "outbound", now)

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["last_message_body"] == "latest reply"
    assert body[0]["last_message_direction"] == "outbound"
    assert body[0]["state"] == "HUMAN_HANDOFF"


def test_excludes_merchants_own_number(client, db_session, owner_and_account):
    owner, account, headers = owner_and_account
    _message(db_session, account, owner.username, "/manage", "inbound", datetime.utcnow())
    _message(db_session, account, "255722220005", "hi", "inbound", datetime.utcnow())

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    phones = [c["customer_phone"] for c in resp.json()]
    assert phones == ["255722220005"]


def test_excludes_conversations_belonging_to_other_accounts(client, db_session, owner_and_account):
    _, account, headers = owner_and_account

    other_owner = User(username="255700000901")
    db_session.add(other_owner)
    db_session.commit()
    db_session.refresh(other_owner)
    other_account = WhatsAppAccount(
        name="Other Shop", phone_number_id="OTHER_CONV_PHONE", access_token="tok", user_id=other_owner.id,
    )
    db_session.add(other_account)
    db_session.commit()
    db_session.refresh(other_account)
    _message(db_session, other_account, "255722220006", "hi", "inbound", datetime.utcnow())

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []
