from app.db.models import (
    ConversationContext,
    Customer,
    Order,
    Product,
)
from app.services.llm import (
    LLMService,
    _parse_and_strip_quote,
)


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


class _FakeContentBlock:
    def __init__(self, text=None, type="text"):
        self.type = type
        if text is not None:
            self.text = text


class _FakeResponse:
    def __init__(self, text, thinking_first=False):
        blocks = []
        if thinking_first:
            blocks.append(_FakeContentBlock(type="thinking"))
        blocks.append(_FakeContentBlock(text=text))
        self.content = blocks


def _service_with_fake_client(handler=None, captured=None):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse("Sure, here is some info.")

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


async def test_quote_marker_tracked_and_nudge_scheduled(db_session, shop_account, shoes, monkeypatch):
    scheduled = {}

    def fake_apply_async(args, countdown):
        scheduled["args"] = args
        scheduled["countdown"] = countdown

    from app.tasks.quote_nudges import send_quote_nudge, QUOTE_NUDGE_INTERVALS_SECONDS

    monkeypatch.setattr(send_quote_nudge, "apply_async", fake_apply_async)

    async def handler(**kwargs):
        return _FakeResponse("I can do TZS 45,000. [QUOTE: product=Shoes price=45000]")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="how much for the shoes?",
        from_phone="255711140001",
        db=db_session,
        account=shop_account,
    )

    assert "QUOTE" not in result
    assert result == "I can do TZS 45,000."

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140001").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.current_product_intent == "Shoes"
    assert context.pending_quote_price == 45000.0
    assert context.pending_quote_at is not None
    assert context.quote_nudge_count == 0

    assert scheduled["args"] == [str(context.id), str(shop_account.id), context.pending_quote_at.isoformat(), 1]
    assert scheduled["countdown"] == QUOTE_NUDGE_INTERVALS_SECONDS[0]


async def test_quote_for_unknown_product_is_not_tracked(db_session, shop_account, shoes, monkeypatch):
    from app.tasks.quote_nudges import send_quote_nudge

    called = []
    monkeypatch.setattr(send_quote_nudge, "apply_async", lambda *a, **kw: called.append(1))

    async def handler(**kwargs):
        return _FakeResponse("Sure. [QUOTE: product=NonexistentThing price=1000]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="how much?",
        from_phone="255711140002",
        db=db_session,
        account=shop_account,
    )

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140002").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.pending_quote_at is None
    assert not called


async def test_quote_superseded_by_agreement_same_turn_not_tracked(db_session, shop_account, shoes):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [QUOTE: product=Shoes price=45000] "
            "[AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it for 45000 full",
        from_phone="255711140003",
        db=db_session,
        account=shop_account,
    )

    assert "QUOTE" not in result
    assert "AGREEMENT_REACHED" not in result

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140003").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    # Deal closed -> real order, no pending-quote nudge needed.
    assert context.pending_quote_at is None
    assert db_session.query(Order).count() == 1


async def test_quote_superseded_by_handoff_same_turn_not_tracked(db_session, shop_account, shoes, monkeypatch):
    async def fake_alert(self, to, name, language_code="en_US", components=None):
        return {"messages": [{"id": "wamid.alert"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", fake_alert)

    async def handler(**kwargs):
        return _FakeResponse(
            "Let me get a human. [QUOTE: product=Shoes price=45000] [HUMAN_HANDOFF]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="I want to talk to someone",
        from_phone="255711140004",
        db=db_session,
        account=shop_account,
    )

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140004").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.pending_quote_at is None


async def test_quote_ignored_on_platform_account(db_session, platform_account, merchant):
    db_session.add(
        Product(slug="platform-widget2", name="Widget", max_price=100, min_price=50, quantity=5, merchant_id=merchant.id)
    )
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse("Sure. [QUOTE: product=Widget price=75]")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="how much",
        from_phone="255711140005",
        db=db_session,
        account=platform_account,
    )

    assert "QUOTE" not in result

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140005").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.pending_quote_at is None


async def test_pending_quote_cleared_proactively_on_next_message(db_session, shop_account, shoes):
    customer = Customer(phone_number="255711140006")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    context = ConversationContext(
        customer_id=customer.id,
        current_product_intent="Shoes",
        pending_quote_price=45000.0,
        pending_quote_at=datetime_now_utc_naive(),
        quote_nudge_count=2,
    )
    db_session.add(context)
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse("Hello again!")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="hey",
        from_phone="255711140006",
        db=db_session,
        account=shop_account,
    )

    db_session.refresh(context)
    assert context.pending_quote_price is None
    assert context.pending_quote_at is None
    assert context.quote_nudge_count == 0


def datetime_now_utc_naive():
    from datetime import datetime
    return datetime.utcnow()


def test_parse_and_strip_quote_malformed_price_returns_none():
    # "45.00.00" matches the regex's [\d.]+ character class (so the marker
    # IS recognized and stripped) but still fails float() conversion.
    text, quote = _parse_and_strip_quote("Sure. [QUOTE: product=Shoes price=45.00.00]")
    assert quote is None
    assert "QUOTE" not in text


def test_parse_and_strip_quote_whitespace_only_product_returns_none():
    text, quote = _parse_and_strip_quote("Sure. [QUOTE: product=  price=45000]")
    assert quote is None
    assert "QUOTE" not in text


async def test_quote_schedule_failure_does_not_break_reply(db_session, shop_account, shoes, monkeypatch):
    from app.tasks.quote_nudges import send_quote_nudge

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(send_quote_nudge, "apply_async", raising_apply_async)

    async def handler(**kwargs):
        return _FakeResponse("I can do TZS 45,000. [QUOTE: product=Shoes price=45000]")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="how much for the shoes?",
        from_phone="255711140007",
        db=db_session,
        account=shop_account,
    )

    assert result == "I can do TZS 45,000."
    customer = db_session.query(Customer).filter(Customer.phone_number == "255711140007").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    # State was still recorded even though scheduling the nudge failed.
    assert context.pending_quote_price == 45000.0


# --- Usage ledger (#41) ------------------------------------------------------------


async def test_quote_is_recorded_for_stats(db_session, shop_account, shoes):
    from app.db.models import AmaraEvent, AmaraEventKind

    async def handler(**kwargs):
        return _FakeResponse("I can do TZS 45,000. [QUOTE: product=shoes price=45000]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="how much?", from_phone="255711149001", db=db_session, account=shop_account,
    )

    quote = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.QUOTE).one()
    assert (quote.merchant_id, quote.product_name, quote.amount, quote.channel) == (
        shop_account.user_id, "Shoes", 45000, "whatsapp"
    )


async def test_handoff_is_recorded_for_stats(db_session, shop_account):
    from app.db.models import AmaraEvent, AmaraEventKind

    async def handler(**kwargs):
        return _FakeResponse("Let me get someone. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="I want a human", from_phone="255711149002", db=db_session, account=shop_account,
    )

    handoff = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.HANDOFF).one()
    assert (handoff.merchant_id, handoff.channel) == (shop_account.user_id, "whatsapp")
