from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.db.models import ConversationContext, ConversationState, Customer, Message, User, WhatsAppAccount

URL = "/api/v1/whatsapp/handoffs"


@pytest.fixture
def owner_and_account(db_session):
    owner = User(username="255700000800")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)

    account = WhatsAppAccount(
        name="Handoff Shop", phone_number_id="HANDOFF_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    headers = {"Authorization": f"Bearer {create_access_token(owner.id)}"}
    return owner, account, headers


def _headers_for(db_session):
    other = User(username="255700000801")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def test_requires_auth(client, owner_and_account):
    _, account, _ = owner_and_account

    resp = client.get(URL, headers={"X-WhatsApp-Account-ID": str(account.id)})

    assert resp.status_code in (401, 403)


def test_empty_when_no_pending_handoffs(client, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_lists_pending_handoff_for_this_account(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    customer = Customer(phone_number="255722229090")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone=customer.phone_number, type="text", body="need a human",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    db_session.add(
        ConversationContext(
            customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["customer_phone"] == "255722229090"
    assert body[0]["last_message_body"] == "need a human"
    assert body[0]["handoff_at"] is not None


def test_excludes_handoffs_belonging_to_other_accounts(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    other_owner = User(username="255700000802")
    db_session.add(other_owner)
    db_session.commit()
    db_session.refresh(other_owner)
    other_account = WhatsAppAccount(
        name="Other Shop", phone_number_id="OTHER_HANDOFF_PHONE", access_token="tok", user_id=other_owner.id,
    )
    db_session.add(other_account)
    db_session.commit()
    db_session.refresh(other_account)

    customer = Customer(phone_number="255722229091")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone=customer.phone_number, type="text", body="hi",
            direction="inbound", status="sent", account_id=other_account.id,
        )
    )
    db_session.add(ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF))
    db_session.commit()

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_excludes_negotiating_conversations(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    customer = Customer(phone_number="255722229092")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone=customer.phone_number, type="text", body="hi",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    db_session.add(ConversationContext(customer_id=customer.id, state=ConversationState.NEGOTIATING))
    db_session.commit()

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def _seed_handoff(db_session, account, phone, state=ConversationState.HUMAN_HANDOFF):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone=phone, type="text", body="hi",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    db_session.add(ConversationContext(customer_id=customer.id, state=state, handoff_at=datetime.utcnow()))
    db_session.commit()
    return customer


def test_resume_requires_auth(client, owner_and_account):
    _, account, _ = owner_and_account

    resp = client.post(
        f"{URL}/255722229093/resume", headers={"X-WhatsApp-Account-ID": str(account.id)}
    )

    assert resp.status_code in (401, 403)


def test_resume_succeeds_and_flips_state_back_to_negotiating(
    client, db_session, owner_and_account, monkeypatch
):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}
    _seed_handoff(db_session, account, "255722229093")
    calls = []
    monkeypatch.setattr(
        "app.tasks.webhook_processing.process_inbound_message.apply_async",
        lambda *a, **kw: calls.append(kw.get("args")),
    )

    resp = client.post(f"{URL}/255722229093/resume", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "resumed", "phone": "255722229093", "resume_reply": "queued"}
    customer = db_session.query(Customer).filter(Customer.phone_number == "255722229093").one()
    context = (
        db_session.query(ConversationContext)
        .filter(ConversationContext.customer_id == customer.id)
        .one()
    )
    assert context.state == ConversationState.NEGOTIATING
    assert context.handoff_at is None
    assert calls == [[str(account.id), "255722229093", "hi"]]


def test_resume_with_stale_last_message_skips_takeover_reply(
    client, db_session, owner_and_account, monkeypatch
):
    from datetime import timedelta

    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}
    _seed_handoff(db_session, account, "255722229093")
    msg = (
        db_session.query(Message)
        .filter(Message.from_phone == "255722229093", Message.direction == "inbound")
        .one()
    )
    msg.timestamp = datetime.utcnow() - timedelta(hours=25)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        "app.tasks.webhook_processing.process_inbound_message.apply_async",
        lambda *a, **kw: calls.append(kw.get("args")),
    )

    resp = client.post(f"{URL}/255722229093/resume", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "resumed", "phone": "255722229093", "resume_reply": "skipped_stale",
    }
    assert calls == []


def test_resume_404_when_no_conversation(client, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    resp = client.post(f"{URL}/255700009999/resume", headers=headers)

    assert resp.status_code == 404


def test_resume_404_when_history_belongs_to_a_different_account(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}

    other_owner = User(username="255700000803")
    db_session.add(other_owner)
    db_session.commit()
    db_session.refresh(other_owner)
    other_account = WhatsAppAccount(
        name="Other Shop 2", phone_number_id="OTHER_HANDOFF_PHONE_2", access_token="tok", user_id=other_owner.id,
    )
    db_session.add(other_account)
    db_session.commit()
    db_session.refresh(other_account)
    _seed_handoff(db_session, other_account, "255722229094")

    resp = client.post(f"{URL}/255722229094/resume", headers=headers)

    assert resp.status_code == 404


def test_resume_400_when_not_in_handoff_state(client, db_session, owner_and_account):
    _, account, headers = owner_and_account
    headers = {**headers, "X-WhatsApp-Account-ID": str(account.id)}
    _seed_handoff(db_session, account, "255722229095", state=ConversationState.NEGOTIATING)

    resp = client.post(f"{URL}/255722229095/resume", headers=headers)

    assert resp.status_code == 400
