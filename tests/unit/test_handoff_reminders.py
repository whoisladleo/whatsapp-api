import uuid
from datetime import datetime

import pytest

import app.tasks.handoff_reminders as handoff_reminders
from app.core.security import create_access_token
from app.db.models import ConversationContext, ConversationState, Customer, Message, User, WhatsAppAccount


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(handoff_reminders, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000700")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Reminder Shop", phone_number_id="REMINDER_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def handed_off_context(db_session, account):
    customer = Customer(phone_number="255722229999")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    db_session.add(
        Message(
            from_phone=customer.phone_number, type="text", body="help",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    context = ConversationContext(
        customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return context


def test_reminder_for_missing_context():
    result = handoff_reminders.send_handoff_reminder(999999)

    assert result == "Context not found or no longer awaiting handoff."


def test_reminder_noop_when_already_resumed(db_session, handed_off_context):
    handed_off_context.state = ConversationState.NEGOTIATING
    db_session.commit()

    result = handoff_reminders.send_handoff_reminder(handed_off_context.id)

    assert result == "Context not found or no longer awaiting handoff."


def test_reminder_notifies_merchant(db_session, handed_off_context, owner, monkeypatch):
    owner_username = owner.username

    async def fake_send_text(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.reminder"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    result = handoff_reminders.send_handoff_reminder(handed_off_context.id)

    assert result == "Success"
    alert = (
        db_session.query(Message)
        .filter(Message.to_phone == owner_username, Message.direction == "outbound")
        .first()
    )
    assert alert is not None
    assert "still waiting" in alert.body


def test_reminder_send_failure_logs_failed_status(db_session, handed_off_context, owner, monkeypatch):
    owner_username = owner.username

    async def failing_send_text(self, to, body, preview_url=False):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", failing_send_text)

    result = handoff_reminders.send_handoff_reminder(handed_off_context.id)

    assert result == "Success"
    alert = (
        db_session.query(Message)
        .filter(Message.to_phone == owner_username, Message.direction == "outbound")
        .first()
    )
    assert alert is not None
    assert alert.status == "failed"


def test_reminder_no_customer_linked(db_session):
    # A context row with no matching customer row (orphaned FK, allowed
    # since SQLite doesn't enforce FKs by default in this test DB).
    orphan = ConversationContext(customer_id=uuid.uuid4(), state=ConversationState.HUMAN_HANDOFF)
    db_session.add(orphan)
    db_session.commit()
    db_session.refresh(orphan)

    result = handoff_reminders.send_handoff_reminder(orphan.id)

    assert result == "No customer linked to this context."


def test_reminder_no_inbound_history_for_any_account(db_session):
    customer = Customer(phone_number="255722228888")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    result = handoff_reminders.send_handoff_reminder(context.id)

    assert result == "No account found for this customer."


def test_reminder_account_owner_missing(db_session):
    orphan_account = WhatsAppAccount(
        name="Orphan", phone_number_id="ORPHAN_PHONE", access_token="tok", user_id=uuid.uuid4(),
    )
    db_session.add(orphan_account)
    db_session.commit()
    db_session.refresh(orphan_account)

    customer = Customer(phone_number="255722227777")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone=customer.phone_number, type="text", body="hi",
            direction="inbound", status="sent", account_id=orphan_account.id,
        )
    )
    context = ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    result = handoff_reminders.send_handoff_reminder(context.id)

    assert result == "No merchant to notify."
