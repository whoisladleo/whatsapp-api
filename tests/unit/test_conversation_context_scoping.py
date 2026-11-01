"""Per-account conversation-state scoping (findings 1 + 11).

ConversationContext used to be one row per customer phone globally: a
handoff at shop A silenced the bot at shop B, and chatting at B cleared
A's pending quote (killing A's nudge cascade). State is now one row per
(customer, WhatsApp account), with legacy NULL-account rows adopted by
the first account that sees them.
"""
from datetime import datetime

import pytest
from sqlalchemy import event

import app.tasks.handoff_reminders as handoff_reminders
import app.tasks.quote_nudges as quote_nudges
from app.crud import message as crud_message
from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Message,
    User,
    WhatsAppAccount,
)
from app.services.handoff_flow import list_pending_handoffs, target_still_pending
from app.services.llm import LLMService

PHONE = "255711110001"


@pytest.fixture
def merchant_a(db_session):
    user = User(username="255700000101")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def merchant_b(db_session):
    user = User(username="255700000102")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account_a(db_session, merchant_a):
    account = WhatsAppAccount(
        name="Shop A", phone_number_id="SCOPE_PHONE_A", access_token="tok", user_id=merchant_a.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def account_b(db_session, merchant_b):
    account = WhatsAppAccount(
        name="Shop B", phone_number_id="SCOPE_PHONE_B", access_token="tok", user_id=merchant_b.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number=PHONE)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def reply_service(monkeypatch):
    """generate_response with the model stubbed to a plain no-marker reply."""
    service = LLMService(api_key=None)
    service.client = object()

    async def fake_complete(*args, **kwargs):
        return "Sure, happy to help with that."

    monkeypatch.setattr(
        "app.services.llm.replies_wa._complete_with_retry", fake_complete
    )
    return service


def _inbound(db_session, account, phone, body="hello"):
    db_session.add(
        Message(
            from_phone=phone, type="text", body=body,
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    db_session.commit()


def _contexts_for(db_session, customer):
    return (
        db_session.query(ConversationContext)
        .filter(ConversationContext.customer_id == customer.id)
        .all()
    )


# --- generate_response isolation ----------------------------------------


async def test_handoff_at_account_a_does_not_silence_account_b(
    db_session, account_a, account_b, customer, reply_service
):
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    reply_b = await reply_service.generate_response(
        message_body="hi", from_phone=PHONE, db=db_session, account=account_b
    )
    reply_a = await reply_service.generate_response(
        message_body="hi", from_phone=PHONE, db=db_session, account=account_a
    )

    assert reply_b == "Sure, happy to help with that."
    assert reply_a is None
    assert len(_contexts_for(db_session, customer)) == 2


async def test_quote_at_a_survives_chat_at_b(
    db_session, account_a, account_b, customer, reply_service
):
    quoted_at = datetime.utcnow()
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            current_product_intent="Shoes", pending_quote_price=45000.0,
            pending_quote_at=quoted_at, quote_nudge_count=0,
        )
    )
    db_session.commit()

    await reply_service.generate_response(
        message_body="hi", from_phone=PHONE, db=db_session, account=account_b
    )

    context_a = (
        db_session.query(ConversationContext)
        .filter(
            ConversationContext.customer_id == customer.id,
            ConversationContext.account_id == account_a.id,
        )
        .one()
    )
    assert context_a.pending_quote_price == 45000.0
    assert context_a.pending_quote_at == quoted_at
    assert len(_contexts_for(db_session, customer)) == 2


async def test_legacy_null_context_adopted_by_first_account(
    db_session, account_a, account_b, customer, reply_service
):
    db_session.add(ConversationContext(customer_id=customer.id))
    db_session.commit()

    reply = await reply_service.generate_response(
        message_body="hi", from_phone=PHONE, db=db_session, account=account_a
    )

    assert reply == "Sure, happy to help with that."
    contexts = _contexts_for(db_session, customer)
    assert len(contexts) == 1
    assert contexts[0].account_id == account_a.id


async def test_legacy_handoff_adopted_and_still_silences(
    db_session, account_a, account_b, customer, reply_service
):
    db_session.add(
        ConversationContext(
            customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    assert (
        await reply_service.generate_response(
            message_body="hi", from_phone=PHONE, db=db_session, account=account_a
        )
    ) is None
    contexts = _contexts_for(db_session, customer)
    assert len(contexts) == 1
    assert contexts[0].account_id == account_a.id
    assert contexts[0].state == ConversationState.HUMAN_HANDOFF


# --- crud scoping ---------------------------------------------------------


def test_get_or_create_context_reuses_scoped_row(db_session, account_a, account_b, customer):
    first = crud_message.get_or_create_context(db_session, customer.id, account_a.id)
    same = crud_message.get_or_create_context(db_session, customer.id, account_a.id)
    other = crud_message.get_or_create_context(db_session, customer.id, account_b.id)

    assert same.id == first.id
    assert other.id != first.id
    assert len(_contexts_for(db_session, customer)) == 2


def test_resume_scoped_per_account(db_session, account_a, account_b, customer):
    _inbound(db_session, account_a, PHONE)
    _inbound(db_session, account_b, PHONE)
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    # Shop B has history but no handoff of its own — nothing to resume there.
    with pytest.raises(crud_message.ConversationNotInHandoffError):
        crud_message.resume_conversation_for_account(db_session, account_b.id, PHONE)

    crud_message.resume_conversation_for_account(db_session, account_a.id, PHONE)
    context_a = crud_message.get_context_for_account(db_session, customer.id, account_a.id)
    assert context_a.state == ConversationState.NEGOTIATING
    assert context_a.handoff_at is None


def test_resume_adopts_legacy_null_row(db_session, account_a, customer):
    _inbound(db_session, account_a, PHONE)
    db_session.add(
        ConversationContext(
            customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    crud_message.resume_conversation_for_account(db_session, account_a.id, PHONE)

    contexts = _contexts_for(db_session, customer)
    assert len(contexts) == 1
    assert contexts[0].account_id == account_a.id
    assert contexts[0].state == ConversationState.NEGOTIATING


def test_conversations_list_state_is_per_account(db_session, account_a, account_b, customer):
    _inbound(db_session, account_a, PHONE, body="help me")
    _inbound(db_session, account_b, PHONE, body="just browsing")
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.add(
        ConversationContext(customer_id=customer.id, account_id=account_b.id)
    )
    db_session.commit()

    convos_a = crud_message.get_conversations_for_account(db_session, account_a.id)
    convos_b = crud_message.get_conversations_for_account(db_session, account_b.id)

    assert [c["state"] for c in convos_a if c["customer_phone"] == PHONE] == ["HUMAN_HANDOFF"]
    assert [c["state"] for c in convos_b if c["customer_phone"] == PHONE] == ["NEGOTIATING"]


# --- finding 11: pending handoffs scoped by account first -----------------


def _handoff_seed(db_session, account, phone, state=ConversationState.HUMAN_HANDOFF, body="need a human"):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    _inbound(db_session, account, phone, body=body)
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account.id,
            state=state, handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    return customer


def test_get_pending_handoffs_scoped_by_account(db_session, account_a, account_b, customer):
    # Same phone chatted at both shops; only shop A's side is waiting.
    _inbound(db_session, account_a, PHONE, body="need a human at A")
    _inbound(db_session, account_b, PHONE, body="just browsing at B")
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.add(
        ConversationContext(customer_id=customer.id, account_id=account_b.id)
    )
    _handoff_seed(db_session, account_b, "255711110002", body="need a human at B")
    db_session.commit()

    pending_a = crud_message.get_pending_handoffs_for_account(db_session, account_a.id)
    pending_b = crud_message.get_pending_handoffs_for_account(db_session, account_b.id)

    assert [p["customer_phone"] for p in pending_a] == [PHONE]
    assert pending_a[0]["last_message_body"] == "need a human at A"
    assert pending_a[0]["handoff_at"] is not None
    assert [p["customer_phone"] for p in pending_b] == ["255711110002"]
    assert pending_b[0]["last_message_body"] == "need a human at B"


def test_get_pending_handoffs_still_sees_legacy_null_rows(db_session, account_a, account_b, customer):
    _inbound(db_session, account_a, PHONE, body="need a human")
    db_session.add(
        ConversationContext(
            customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    assert [p["customer_phone"] for p in crud_message.get_pending_handoffs_for_account(db_session, account_a.id)] == [PHONE]
    # No inbound on shop B at all — not B's handoff.
    assert crud_message.get_pending_handoffs_for_account(db_session, account_b.id) == []


def test_get_pending_handoffs_query_count_independent_of_row_count(db_session, account_a):
    def count_queries(fn):
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db_session.bind, "before_cursor_execute", record)
        try:
            fn()
        finally:
            event.remove(db_session.bind, "before_cursor_execute", record)
        return len(statements)

    for i in range(3):
        _handoff_seed(db_session, account_a, f"25571112000{i}")
    small = count_queries(
        lambda: crud_message.get_pending_handoffs_for_account(db_session, account_a.id)
    )

    for i in range(3, 13):
        _handoff_seed(db_session, account_a, f"2557111200{i:02d}")
    large = count_queries(
        lambda: crud_message.get_pending_handoffs_for_account(db_session, account_a.id)
    )

    # The old implementation issued one latest-inbound query per handoff
    # context; the rewrite must not grow with the handoff count.
    assert small == large
    assert len(crud_message.get_pending_handoffs_for_account(db_session, account_a.id)) == 13


# --- handoff flow + tasks -------------------------------------------------


def test_list_pending_handoffs_scoped_per_account(
    db_session, merchant_a, merchant_b, account_a, account_b, customer
):
    _inbound(db_session, account_a, PHONE)
    _inbound(db_session, account_b, PHONE)
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_b.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    # Merchant A has history with this phone, but the waiting row is B's.
    assert list_pending_handoffs(db_session, merchant_a) == []
    pending_b = list_pending_handoffs(db_session, merchant_b)
    assert [p["ref"] for p in pending_b] == [PHONE]


def test_target_still_pending_scoped_per_account(db_session, account_a, account_b, customer):
    _inbound(db_session, account_a, PHONE)
    _inbound(db_session, account_b, PHONE)
    db_session.add(
        ConversationContext(
            customer_id=customer.id, account_id=account_a.id,
            state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
        )
    )
    db_session.add(
        ConversationContext(customer_id=customer.id, account_id=account_b.id)
    )
    db_session.commit()

    target_a = {"channel": "whatsapp", "ref": PHONE, "account_id": str(account_a.id)}
    target_b = {"channel": "whatsapp", "ref": PHONE, "account_id": str(account_b.id)}

    assert target_still_pending(db_session, target_a) is True
    assert target_still_pending(db_session, target_b) is False

    crud_message.resume_conversation_for_account(db_session, account_a.id, PHONE)
    assert target_still_pending(db_session, target_a) is False


def test_handoff_reminder_routes_via_context_account(
    db_session, merchant_a, account_a, customer, monkeypatch
):
    monkeypatch.setattr(handoff_reminders, "SessionLocal", lambda: db_session)

    async def fake_send_text(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.scopedreminder"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    # No inbound messages anywhere — the scoped row alone must route the alert.
    context = ConversationContext(
        customer_id=customer.id, account_id=account_a.id,
        state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    # The task closes the shared test session — capture everything first.
    context_id, merchant_phone = context.id, merchant_a.username

    assert handoff_reminders.send_handoff_reminder(context_id) == "Success"
    alert = (
        db_session.query(Message)
        .filter(Message.to_phone == merchant_phone, Message.direction == "outbound")
        .first()
    )
    assert alert is not None
    assert "still waiting" in alert.body


def test_quote_nudge_adopts_legacy_row_then_skips_other_account(
    db_session, account_a, account_b, customer, monkeypatch
):
    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)

    async def fake_send_text(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.scopednudge"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    context = ConversationContext(
        customer_id=customer.id, current_product_intent="Shoes",
        pending_quote_price=45000.0, pending_quote_at=datetime.utcnow(),
        quote_nudge_count=0,
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    context_id = context.id
    account_a_id, account_b_id = account_a.id, account_b.id
    quote_iso = context.pending_quote_at.isoformat()

    assert quote_nudges.send_quote_nudge(context_id, account_a_id, quote_iso, 1) == "Success"
    # The task closes the shared test session — re-query rather than refresh.
    adopted = db_session.get(ConversationContext, context_id)
    assert adopted.account_id == account_a_id

    # Same scheduled leg, but the row now belongs to shop A — shop B's copy
    # of the schedule must stay silent.
    assert quote_nudges.send_quote_nudge(context_id, account_b_id, quote_iso, 2) == (
        "Quote moved to a different account; skipping stale nudge."
    )
