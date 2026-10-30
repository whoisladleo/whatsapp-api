import uuid
from datetime import datetime

import pytest

import app.tasks.quote_nudges as quote_nudges
from app.core.config import settings
from app.db.models import ConversationContext, Customer, Message, Product, User, WhatsAppAccount


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000900")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Quote Nudge Shop", phone_number_id="QUOTE_NUDGE_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255722228888")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def quoted_context(db_session, customer):
    context = ConversationContext(
        customer_id=customer.id,
        current_product_intent="Shoes",
        pending_quote_price=45000.0,
        pending_quote_at=datetime.utcnow(),
        quote_nudge_count=0,
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return context


def test_noop_when_context_missing():
    result = quote_nudges.send_quote_nudge(999999, 1, "2026-01-01T00:00:00", 1)
    assert result == "No pending quote for this context."


def test_noop_when_no_pending_quote(db_session, quoted_context):
    quoted_context.pending_quote_at = None
    db_session.commit()

    result = quote_nudges.send_quote_nudge(quoted_context.id, 1, "2026-01-01T00:00:00", 1)

    assert result == "No pending quote for this context."


def test_noop_when_quote_timestamp_superseded(db_session, quoted_context, account):
    stale_iso = "2020-01-01T00:00:00"  # doesn't match the real pending_quote_at

    result = quote_nudges.send_quote_nudge(quoted_context.id, account.id, stale_iso, 1)

    assert result == "Quote was superseded by a newer one; skipping stale nudge."


def test_noop_when_nudge_count_already_moved_on(db_session, quoted_context, account):
    quoted_context.quote_nudge_count = 3
    db_session.commit()

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Nudge count has moved on; skipping stale nudge."


def test_noop_when_account_not_found(db_session, quoted_context):
    result = quote_nudges.send_quote_nudge(
        quoted_context.id, 999999, quoted_context.pending_quote_at.isoformat(), 1
    )
    assert result == "Account not found."


def test_first_nudge_sends_increments_and_reschedules(db_session, quoted_context, account, monkeypatch):
    sent = {}

    async def fake_send_text(self, to, body, preview_url=False):
        sent["to"] = to
        sent["body"] = body
        return {"messages": [{"id": "wamid.nudge1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    rescheduled = {}

    def fake_apply_async(args, countdown):
        rescheduled["args"] = args
        rescheduled["countdown"] = countdown

    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", fake_apply_async)

    context_id = quoted_context.id
    account_id = account.id
    quote_iso = quoted_context.pending_quote_at.isoformat()

    result = quote_nudges.send_quote_nudge(context_id, account_id, quote_iso, 1)

    assert result == "Success"
    assert sent["to"] == "255722228888"
    assert "Shoes" in sent["body"]

    updated_context = db_session.query(ConversationContext).filter(
        ConversationContext.id == context_id
    ).first()
    assert updated_context.quote_nudge_count == 1

    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == "255722228888")
        .first()
    )
    assert outbound is not None
    assert outbound.status == "sent"

    assert rescheduled["args"] == [str(context_id), str(account_id), quote_iso, 2]
    assert rescheduled["countdown"] == quote_nudges.QUOTE_NUDGE_INTERVALS_SECONDS[1]


def test_fifth_nudge_does_not_reschedule(db_session, quoted_context, account, monkeypatch):
    quoted_context.quote_nudge_count = 4  # about to become nudge_number=5
    db_session.commit()

    async def fake_send_text(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.nudge5"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    called = []
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: called.append(1))

    context_id = quoted_context.id
    account_id = account.id
    quote_iso = quoted_context.pending_quote_at.isoformat()

    result = quote_nudges.send_quote_nudge(context_id, account_id, quote_iso, 5)

    assert result == "Success"
    updated_context = db_session.query(ConversationContext).filter(
        ConversationContext.id == context_id
    ).first()
    assert updated_context.quote_nudge_count == 5
    assert not called  # no nudge 6


def test_send_failure_still_increments_and_reschedules(db_session, quoted_context, account, monkeypatch):
    async def failing_send_text(self, to, body, preview_url=False):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", failing_send_text)

    rescheduled = []
    monkeypatch.setattr(
        quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: rescheduled.append(1)
    )

    context_id = quoted_context.id
    account_id = account.id
    quote_iso = quoted_context.pending_quote_at.isoformat()

    result = quote_nudges.send_quote_nudge(context_id, account_id, quote_iso, 1)

    assert result == "Success"
    updated_context = db_session.query(ConversationContext).filter(
        ConversationContext.id == context_id
    ).first()
    assert updated_context.quote_nudge_count == 1
    assert rescheduled

    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == "255722228888")
        .first()
    )
    assert outbound.status == "failed"


def test_reschedule_failure_does_not_break_the_send(db_session, quoted_context, account, monkeypatch):
    async def fake_send_text(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.nudge1"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", raising_apply_async)

    context_id = quoted_context.id
    account_id = account.id
    quote_iso = quoted_context.pending_quote_at.isoformat()

    result = quote_nudges.send_quote_nudge(context_id, account_id, quote_iso, 1)

    assert result == "Success"
    updated_context = db_session.query(ConversationContext).filter(
        ConversationContext.id == context_id
    ).first()
    assert updated_context.quote_nudge_count == 1


def test_no_customer_linked(db_session, account):
    # customer_id points at a row that doesn't exist -> context.customer is None
    orphan_context = ConversationContext(
        customer_id=uuid.uuid4(),
        pending_quote_price=1000.0,
        pending_quote_at=datetime.utcnow(),
        quote_nudge_count=0,
    )
    db_session.add(orphan_context)
    db_session.commit()
    db_session.refresh(orphan_context)

    result = quote_nudges.send_quote_nudge(
        orphan_context.id, account.id, orphan_context.pending_quote_at.isoformat(), 1
    )

    assert result == "No customer linked to this context."


def test_product_name_falls_back_to_generic_when_missing(db_session, customer, account, monkeypatch):
    context = ConversationContext(
        customer_id=customer.id,
        current_product_intent=None,
        pending_quote_price=1000.0,
        pending_quote_at=datetime.utcnow(),
        quote_nudge_count=0,
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    sent = {}

    async def fake_send_text(self, to, body, preview_url=False):
        sent["body"] = body
        return {"messages": [{"id": "wamid.x"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    quote_nudges.send_quote_nudge(context.id, account.id, context.pending_quote_at.isoformat(), 1)

    assert "that item" in sent["body"]


def _stocked_shoes(db_session, owner, **overrides):
    kwargs = {
        "slug": "shoes", "name": "Shoes", "max_price": 50000.0, "min_price": 40000.0,
        "quantity": 3, "is_active": True, "merchant_id": owner.id,
    }
    kwargs.update(overrides)
    p = Product(**kwargs)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_nudge_attempts_product_card_for_in_stock_product(
    db_session, quoted_context, account, owner, monkeypatch
):
    product = _stocked_shoes(db_session, owner)
    product_id = product.id  # task closes the shared session; read it first
    attempted = {}

    async def fake_card(self, to, prod, fallback_text, body=None, footer=None, catalog_id=None):
        attempted["to"] = to
        attempted["product_id"] = prod.id
        attempted["fallback_text"] = fallback_text
        return {"messages": [{"id": "wamid.card"}]}

    async def exploding_text(self, to, body, preview_url=False):
        raise AssertionError("plain text must not be sent when the card is attempted")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_product_card_or_text", fake_card)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", exploding_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Success"
    assert attempted["to"] == "255722228888"
    assert attempted["product_id"] == product_id
    assert "Shoes" in attempted["fallback_text"]
    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == "255722228888")
        .first()
    )
    assert outbound.status == "sent"
    assert outbound.body == attempted["fallback_text"]


def test_nudge_preserves_text_when_card_fails(
    db_session, quoted_context, account, owner, monkeypatch
):
    """Card rejected by Meta (e.g. catalog not linked to this WABA) → the
    real helper falls back and the customer still gets the nudge line as text."""
    _stocked_shoes(db_session, owner)
    monkeypatch.setattr(settings, "CATALOG_ID", "CAT123")

    async def rejected_card(self, *a, **kw):
        return {"error": {"message": "catalog not connected", "code": 133004}}

    sent = {}

    async def fake_send_text(self, to, body, preview_url=False):
        sent["to"] = to
        sent["body"] = body
        return {"messages": [{"id": "wamid.fallback"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_product_message", rejected_card)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Success"
    assert sent["to"] == "255722228888"
    assert "Shoes" in sent["body"]


def test_nudge_sends_plain_text_for_out_of_stock_product(
    db_session, quoted_context, account, owner, monkeypatch
):
    _stocked_shoes(db_session, owner, quantity=0)
    sent = {}

    async def exploding_card(self, *a, **kw):
        raise AssertionError("no card attempt for an out-of-stock product")

    async def fake_send_text(self, to, body, preview_url=False):
        sent["body"] = body
        return {"messages": [{"id": "wamid.text"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_product_card_or_text", exploding_card)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Success"
    assert "Shoes" in sent["body"]


def test_nudge_sends_plain_text_for_draft_product(
    db_session, quoted_context, account, owner, monkeypatch
):
    # Stocked AND active — only the draft flag holds the card back.
    _stocked_shoes(db_session, owner, is_draft=True)
    sent = {}

    async def exploding_card(self, *a, **kw):
        raise AssertionError("no card attempt for a draft product")

    async def fake_send_text(self, to, body, preview_url=False):
        sent["body"] = body
        return {"messages": [{"id": "wamid.text"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_product_card_or_text", exploding_card)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Success"
    assert "Shoes" in sent["body"]


def test_nudge_sends_plain_text_for_deactivated_product(
    db_session, quoted_context, account, owner, monkeypatch
):
    _stocked_shoes(db_session, owner, is_active=False)
    sent = {}

    async def exploding_card(self, *a, **kw):
        raise AssertionError("no card attempt for a deactivated product")

    async def fake_send_text(self, to, body, preview_url=False):
        sent["body"] = body
        return {"messages": [{"id": "wamid.text"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_product_card_or_text", exploding_card)
    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send_text)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)

    result = quote_nudges.send_quote_nudge(
        quoted_context.id, account.id, quoted_context.pending_quote_at.isoformat(), 1
    )

    assert result == "Success"
    assert "Shoes" in sent["body"]
