"""Channel-parity tests for the #136 unification: one catalog renderer, one
prompt builder per account type, merged close/resend cores, and full IG
behavior parity (language classification, failed-cycle handoff, quote
nudges) plus the platform pitch on Instagram."""

import pytest

from app.core.config import settings
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
)
from app.services.llm import (
    CHANNEL_INSTAGRAM,
    CHANNEL_WHATSAPP,
    LLMService,
    build_merchant_prompt,
    build_platform_prompt,
    render_catalog,
    DealRejected,
    _holding_reply,
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
    def __init__(self, text):
        self.content = [_FakeContentBlock(text=text)]


def _service_with_fake_client(handler=None, captured=None, reply_text="Sawa."):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse(reply_text)

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_parity_1",
        access_token="ig-tok",
        username="parityshop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_parity_1", username="prospect1", account_id=ig_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def ig_context(db_session, ig_customer):
    ctx = InstagramConversationContext(customer_id=ig_customer.id)
    db_session.add(ctx)
    db_session.commit()
    db_session.refresh(ctx)
    return ctx


@pytest.fixture
def ig_platform_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_parity_platform",
        access_token="ig-tok",
        username="taskflow_os",
        ai_enabled=True,
        user_id=merchant.id,
        is_platform_account=True,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def entry_plan(db_session, merchant):
    p = Product(
        slug="parity-entry-plan", name="Parity Entry Plan",
        max_price=100000, min_price=100000, quantity=0,
        merchant_id=merchant.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2000,
        allow_installments=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def wa_customer(db_session):
    c = Customer(phone_number="255700000136")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def wa_context(db_session, wa_customer, shop_account):
    ctx = ConversationContext(customer_id=wa_customer.id, account_id=shop_account.id)
    db_session.add(ctx)
    db_session.commit()
    db_session.refresh(ctx)
    return ctx


def _product(**kw):
    base = dict(
        slug="parity-p", name="Widget", max_price=50000, min_price=40000,
        quantity=3, is_active=True, allow_installments=True,
        is_subscription=False, is_draft=False, is_platform_plan=False,
        monthly_reply_limit=None, description=None,
    )
    base.update(kw)
    return Product(**base)


# --- render_catalog -----------------------------------------------------

def test_render_catalog_dm_full_line():
    out = render_catalog([_product(description="A fine widget.")])
    assert "Widget: Max Price TZS 50,000" in out
    assert "Lowest Price You May Offer TZS 44,000" in out
    assert "Stock: 3" in out
    assert "A fine widget." in out
    assert "OUT OF STOCK" not in out


def test_render_catalog_dm_subscription_has_no_units():
    out = render_catalog([_product(is_subscription=True, quantity=0)])
    assert "available now, billed monthly" in out
    assert "Stock: 0" not in out
    assert "OUT OF STOCK" not in out


def test_render_catalog_dm_out_of_stock_and_full_payment_only():
    out = render_catalog([_product(quantity=0, allow_installments=False)])
    assert "OUT OF STOCK" in out
    assert "full payment only" in out


def test_render_catalog_dm_plan_note():
    out = render_catalog([_product(is_subscription=True, is_platform_plan=True, monthly_reply_limit=2000)])
    assert "includes 2,000 Amara replies a month" in out
    out_unlimited = render_catalog([_product(is_subscription=True, is_platform_plan=True)])
    assert "includes unlimited Amara replies" in out_unlimited


def test_render_catalog_comment_mode_is_price_and_availability_only():
    out = render_catalog(
        [_product(is_subscription=True, quantity=0, is_platform_plan=True, allow_installments=False)],
        mode="comment",
    )
    assert "Price TZS 50,000" in out
    assert "available now, billed monthly" in out
    # The comment path never closes: no floor, plan allowance, or terms.
    assert "Lowest Price" not in out
    assert "Amara replies" not in out
    assert "full payment only" not in out
    # #135 regression: "Stock: 0" on a plan read as out of stock.
    assert "Stock: 0" not in out


def test_render_catalog_comment_mode_still_marks_dead_stock():
    out = render_catalog([_product(quantity=0)], mode="comment")
    assert "OUT OF STOCK" in out


def test_render_catalog_empty():
    assert render_catalog([]) == "No products currently available."
    assert render_catalog([], mode="comment") == "No products currently available."


# --- prompt parity ------------------------------------------------------

_LOAD_BEARING = [
    "Tanzanian Shillings",
    '"$"',
    "Out of stock: never offer",
    "Payment truth:",
    "Subscription billing is monthly only",
    "Coming back:",
    "[QUOTE:",
    "[HUMAN_HANDOFF]",
    "[RESEND_PAYMENT",
    "[AGREEMENT_REACHED",
]


@pytest.mark.parametrize("channel", [CHANNEL_WHATSAPP, CHANNEL_INSTAGRAM])
def test_merchant_prompts_share_load_bearing_rules(channel):
    prompt = build_merchant_prompt(
        channel=channel, account_name="Shop", catalog_str="Available Products:\n",
    )
    for line in _LOAD_BEARING:
        assert line in prompt, f"{channel} prompt lost: {line}"


def test_merchant_prompts_keep_channel_mechanics():
    wa = build_merchant_prompt(channel=CHANNEL_WHATSAPP, account_name="S", catalog_str="c")
    ig = build_merchant_prompt(channel=CHANNEL_INSTAGRAM, account_name="S", catalog_str="c")
    assert "VOICE:" in wa and "VOICE:" not in ig
    assert "WhatsApp markdown" in wa and "WhatsApp markdown" not in ig
    assert "pickup or delivery" in wa
    assert "Reply with ONLY" in ig and "Reply with ONLY" not in wa
    assert "phone number" in ig
    assert "Never default to Swahili" in ig


def test_merchant_prompt_language_marker_flag():
    with_marker = build_merchant_prompt(
        channel=CHANNEL_INSTAGRAM, account_name="S", catalog_str="c", need_language_marker=True
    )
    without = build_merchant_prompt(
        channel=CHANNEL_INSTAGRAM, account_name="S", catalog_str="c", need_language_marker=False
    )
    assert "[LANGUAGE:" in with_marker
    assert "[LANGUAGE:" not in without


@pytest.mark.parametrize("channel", [CHANNEL_WHATSAPP, CHANNEL_INSTAGRAM])
def test_platform_prompt_welcome_and_fixed_close(channel):
    welcome = build_platform_prompt(
        channel=channel, catalog_str="c", is_first_message=True,
    )
    assert "word-for-word" in welcome
    pitch = build_platform_prompt(
        channel=channel, catalog_str="c", is_first_message=False, multi_plan=True,
    )
    assert "CLOSING MECHANICS" in pitch
    assert "never negotiate it" in pitch
    assert "never offer installments" in pitch
    assert "help them pick the right one" in pitch
    for line in ("Tanzanian Shillings", "Payment truth:", "Coming back:"):
        assert line in pitch


def test_platform_prompt_channel_differences():
    wa = build_platform_prompt(channel=CHANNEL_WHATSAPP, catalog_str="c", is_first_message=False)
    ig = build_platform_prompt(channel=CHANNEL_INSTAGRAM, catalog_str="c", is_first_message=False)
    assert "phone=<phone number>" in ig and "phone=<phone number>" not in wa
    assert settings.DASHBOARD_URL in wa and settings.DASHBOARD_URL in ig
    assert "WhatsApp-length" in wa and "DM-length" in ig


# --- IG language classification -----------------------------------------

async def test_ig_requests_and_persists_language_once(
    db_session, ig_account, ig_customer, ig_context, shoes,
):
    captured = {}
    service = _service_with_fake_client(captured=captured, reply_text="Sawa, bei ni 45,000. [LANGUAGE: sw]")
    result = await service.generate_dm_response(
        "bei gani?",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    assert "[LANGUAGE:" in captured["system"]
    assert "[LANGUAGE:" not in result
    db_session.refresh(ig_customer)
    assert ig_customer.language == "sw"

    captured2 = {}
    service2 = _service_with_fake_client(captured=captured2, reply_text="Ndiyo.")
    await service2.generate_dm_response(
        "sawa",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    assert "[LANGUAGE:" not in captured2["system"]


async def test_ig_holding_reply_uses_classified_language(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    async def _no_alert(*a, **kw):
        return {}

    monkeypatch.setattr(LLMService, "_send_platform_alert_to_merchant", _no_alert)
    ig_customer.language = "sw"
    db_session.commit()
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=999999 payment=full phone=255700000001]"
    )
    result = await service.generate_dm_response(
        "nakubali",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    # Out-of-band price rejects into the holding reply — Swahili only now.
    assert result == _holding_reply("sw")
    assert "someone from our team" not in result


async def test_ig_holding_reply_bilingual_until_classified(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    async def _no_alert(*a, **kw):
        return {}

    monkeypatch.setattr(LLMService, "_send_platform_alert_to_merchant", _no_alert)
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=999999 payment=full phone=255700000001]"
    )
    result = await service.generate_dm_response(
        "I agree",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    assert result == _holding_reply(None)


# --- IG failed cycles + quote tracking -----------------------------------

QUOTE = {"product_name": "Shoes", "price": 44000}


async def test_ig_failed_cycles_hand_off_on_threshold(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    async def _no_alert(*a, **kw):
        return {}

    monkeypatch.setattr(LLMService, "_send_platform_alert_to_merchant", _no_alert)
    service = _service_with_fake_client()
    for _ in range(3):
        await service._track_quote_ig(db_session, ig_account, ig_context, ig_customer, QUOTE, "t")
    db_session.refresh(ig_context)
    assert ig_context.failed_cycles == 2
    assert ig_context.state == ConversationState.NEGOTIATING
    await service._track_quote_ig(db_session, ig_account, ig_context, ig_customer, QUOTE, "t")
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_ig_new_product_resets_failed_cycles(
    db_session, ig_account, ig_customer, ig_context, shoes,
):
    other = Product(
        slug="parity-hat", name="Hat", max_price=20000, min_price=15000,
        quantity=5, merchant_id=ig_account.user_id,
    )
    db_session.add(other)
    db_session.commit()
    service = _service_with_fake_client()
    await service._track_quote_ig(db_session, ig_account, ig_context, ig_customer, QUOTE, "t")
    await service._track_quote_ig(
        db_session, ig_account, ig_context, ig_customer,
        {"product_name": "Hat", "price": 18000}, "t",
    )
    db_session.refresh(ig_context)
    assert ig_context.failed_cycles == 0
    assert ig_context.current_product_intent == "Hat"


async def test_ig_quote_unknown_product_not_tracked(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    from app.tasks.quote_nudges import send_quote_nudge

    calls = []
    monkeypatch.setattr(send_quote_nudge, "apply_async", lambda *a, **kw: calls.append(1))
    service = _service_with_fake_client()
    await service._track_quote_ig(
        db_session, ig_account, ig_context, ig_customer,
        {"product_name": "Nope", "price": 1}, "t",
    )
    assert calls == []
    db_session.refresh(ig_context)
    assert ig_context.pending_quote_at is None


async def test_ig_full_turn_quote_tracks_and_schedules(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    from app.tasks.quote_nudges import send_quote_nudge

    scheduled = []
    monkeypatch.setattr(
        send_quote_nudge, "apply_async", lambda *a, **kw: scheduled.append((a, kw))
    )
    service = _service_with_fake_client(
        reply_text="How about 44,000? [QUOTE: product=Shoes price=44000]"
    )
    await service.generate_dm_response(
        "too high",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    db_session.refresh(ig_context)
    assert ig_context.pending_quote_price == 44000
    assert len(scheduled) == 1
    assert scheduled[0][1]["args"][4] == "instagram_dm"
    event = db_session.query(AmaraEvent).filter(AmaraEvent.kind == AmaraEventKind.QUOTE).one()
    assert event.channel == "instagram_dm"


async def test_ig_inbound_clears_pending_quote(
    db_session, ig_account, ig_customer, ig_context, shoes,
):
    from datetime import datetime

    ig_context.pending_quote_price = 44000
    ig_context.pending_quote_at = datetime.utcnow()
    ig_context.quote_nudge_count = 2
    db_session.commit()
    service = _service_with_fake_client(reply_text="Karibu!")
    await service.generate_dm_response(
        "hello again",
        db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
        conversation_history=[],
    )
    db_session.refresh(ig_context)
    assert ig_context.pending_quote_at is None
    assert ig_context.quote_nudge_count == 0


# --- IG nudge task ---------------------------------------------------------

def _ig_quoted_setup(db_session, merchant):
    from datetime import datetime

    account = InstagramAccount(
        ig_business_id="ig_biz_nudge_1", access_token="tok", username="nudgeshop",
        ai_enabled=True, user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    customer = InstagramCustomer(ig_scoped_id="igsid_nudge_1", username="quiet1", account_id=account.id)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = InstagramConversationContext(
        customer_id=customer.id, current_product_intent="Shoes",
        pending_quote_price=44000.0, pending_quote_at=datetime.utcnow(), quote_nudge_count=0,
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return account, customer, context


def test_ig_nudge_sends_dm_and_reschedules(db_session, merchant, monkeypatch):
    import app.tasks.quote_nudges as quote_nudges
    from app.core.instagram_provider import InstagramProvider

    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)
    sent = []

    async def _fake_send(self, biz, recipient, message, tag=None):
        sent.append({"tag": tag, "message": message})
        return {"message_id": "mid_nudge_1"}

    monkeypatch.setattr(InstagramProvider, "send_direct_message", _fake_send)
    scheduled = []
    monkeypatch.setattr(
        quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: scheduled.append((a, kw))
    )
    account, customer, context = _ig_quoted_setup(db_session, merchant)
    iso = context.pending_quote_at.isoformat()
    ids = {"context": context.id, "account": account.id, "customer": customer.id}

    result = quote_nudges.send_quote_nudge(str(context.id), str(account.id), iso, 1, "instagram_dm")

    assert result == "Success"
    assert len(sent) == 1
    assert sent[0]["tag"] == "HUMAN_AGENT"
    assert "Shoes" in sent[0]["message"]
    # The task closes the session it borrowed: re-query, don't refresh.
    row = db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.customer_id == ids["customer"],
        InstagramDirectMessage.direction == "outbound",
    ).one()
    assert row.ig_message_id == "mid_nudge_1"
    fresh = db_session.query(InstagramConversationContext).filter(
        InstagramConversationContext.id == ids["context"]
    ).one()
    assert fresh.quote_nudge_count == 1
    assert len(scheduled) == 1
    assert scheduled[0][1]["args"][4] == "instagram_dm"
    event = db_session.query(AmaraEvent).filter(AmaraEvent.kind == AmaraEventKind.REPLY).one()
    assert event.channel == "instagram_dm"


def test_ig_nudge_stale_quote_skips(db_session, merchant, monkeypatch):
    import app.tasks.quote_nudges as quote_nudges
    from app.core.instagram_provider import InstagramProvider

    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)

    async def _boom(*a, **kw):
        raise AssertionError("must not send")

    monkeypatch.setattr(InstagramProvider, "send_direct_message", _boom)
    account, _, context = _ig_quoted_setup(db_session, merchant)
    result = quote_nudges.send_quote_nudge(
        str(context.id), str(account.id), "2020-01-01T00:00:00", 1, "instagram_dm"
    )
    assert "superseded" in result


def test_ig_nudge_moved_account_skips(db_session, merchant, monkeypatch):
    import app.tasks.quote_nudges as quote_nudges

    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)
    account, _, context = _ig_quoted_setup(db_session, merchant)
    other = InstagramAccount(
        ig_business_id="ig_biz_nudge_2", access_token="tok", user_id=merchant.id,
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    result = quote_nudges.send_quote_nudge(
        str(context.id), str(other.id), context.pending_quote_at.isoformat(), 1, "instagram_dm"
    )
    assert "different account" in result


def test_ig_nudge_send_failure_still_advances(db_session, merchant, monkeypatch):
    import app.tasks.quote_nudges as quote_nudges
    from app.core.instagram_provider import InstagramProvider

    monkeypatch.setattr(quote_nudges, "SessionLocal", lambda: db_session)

    async def _fail(*a, **kw):
        raise RuntimeError("meta down")

    monkeypatch.setattr(InstagramProvider, "send_direct_message", _fail)
    monkeypatch.setattr(quote_nudges.send_quote_nudge, "apply_async", lambda *a, **kw: None)
    account, customer, context = _ig_quoted_setup(db_session, merchant)
    ids = {"context": context.id, "account": account.id, "customer": customer.id}
    iso = context.pending_quote_at.isoformat()
    result = quote_nudges.send_quote_nudge(
        str(ids["context"]), str(ids["account"]), iso, 1, "instagram_dm"
    )
    assert result == "Success"
    fresh = db_session.query(InstagramConversationContext).filter(
        InstagramConversationContext.id == ids["context"]
    ).one()
    assert fresh.quote_nudge_count == 1
    row = db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.customer_id == ids["customer"],
        InstagramDirectMessage.direction == "outbound",
    ).one()
    assert row.ig_message_id is None
    assert db_session.query(AmaraEvent).filter(AmaraEvent.kind == AmaraEventKind.REPLY).count() == 0


# --- close core ------------------------------------------------------------

async def test_close_validation_keeps_channel_prefixes(
    db_session, shop_account, wa_customer, wa_context, ig_account, ig_customer, shoes,
):
    service = _service_with_fake_client()
    with pytest.raises(DealRejected) as wa_exc:
        await service._close_deal(
            db_session, shop_account, wa_customer, "255700000136",
            {"product_name": "Nope", "price": 1, "payment_type": "full"}, wa_context,
        )
    assert "Instagram" not in str(wa_exc.value)
    with pytest.raises(DealRejected, match="Instagram AGREEMENT_REACHED for unknown product"):
        await service._close_instagram_deal(
            db_session, ig_account, ig_customer,
            {"product_name": "Nope", "price": 1, "payment_type": "full", "phone": "255700000001"},
        )


async def test_ig_close_clears_quote_state_and_copies_language(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    from datetime import datetime

    ig_customer.language = "sw"
    ig_context.pending_quote_price = 44000
    ig_context.pending_quote_at = datetime.utcnow()
    ig_context.quote_nudge_count = 1
    ig_context.failed_cycles = 2
    db_session.commit()
    service = _service_with_fake_client()
    captured = {}

    async def _fake_dispatch(db, order, product, phone, payment_type, language=None):
        captured["language"] = language
        captured["phone"] = phone
        return "PAY-NOTE"

    monkeypatch.setattr(service, "_dispatch_payment", _fake_dispatch)
    note = await service._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Shoes", "price": 45000, "payment_type": "full", "phone": "255700000777"},
        ig_context,
    )
    assert note == "PAY-NOTE"
    assert captured == {"language": "sw", "phone": "255700000777"}
    order = db_session.query(Order).filter(Order.instagram_account_id == ig_account.id).one()
    assert order.status == OrderStatus.PENDING
    db_session.refresh(ig_context)
    assert ig_context.pending_quote_at is None
    assert ig_context.failed_cycles == 0
    assert ig_context.last_offer == 45000
    phone_customer = db_session.query(Customer).filter(
        Customer.phone_number == "255700000777"
    ).one()
    assert phone_customer.language == "sw"


async def test_ig_close_note_prefers_ig_language_without_overwriting_phone(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch,
):
    existing = Customer(phone_number="255700000778", language="en")
    db_session.add(existing)
    db_session.commit()
    ig_customer.language = "sw"
    db_session.commit()
    service = _service_with_fake_client()
    captured = {}

    async def _fake_dispatch(db, order, product, phone, payment_type, language=None):
        captured["language"] = language
        return "PAY-NOTE"

    monkeypatch.setattr(service, "_dispatch_payment", _fake_dispatch)
    await service._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Shoes", "price": 45000, "payment_type": "full", "phone": "255700000778"},
        ig_context,
    )
    assert captured["language"] == "sw"
    db_session.refresh(existing)
    assert existing.language == "en"


# --- resend core -------------------------------------------------------------

def _pending_order(db_session, *, customer_id, product_id, account_id=None,
                   instagram_account_id=None, instagram_customer_id=None, txn="txn_live_1"):
    order = Order(
        customer_id=customer_id, product_id=product_id, account_id=account_id,
        instagram_account_id=instagram_account_id, instagram_customer_id=instagram_customer_id,
        quantity=1, agreed_price=45000, payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING, tunzaa_transaction_id=txn,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


async def test_resend_core_refires_live_order(db_session, shop_account, wa_customer, shoes, monkeypatch):
    order = _pending_order(
        db_session, customer_id=wa_customer.id, product_id=shoes.id, account_id=shop_account.id,
    )
    service = _service_with_fake_client()

    async def _fake_refire(db, order, phone, language=None):
        return "RESENT"

    async def _boom(resend):
        raise AssertionError("must not fresh-close")

    monkeypatch.setattr(service, "_resend_payment_for_order", _fake_refire)
    note = await service._resend_payment_note(
        db=db_session, live_order=order, resend={},
        phone_for_dispatch="255700000136", fallback_phone="255700000136",
        language="en", close_fresh_fn=_boom,
    )
    assert note == "RESENT"


async def test_resend_core_dispatches_onto_undispatched_order(
    db_session, shop_account, wa_customer, shoes, monkeypatch,
):
    order = _pending_order(
        db_session, customer_id=wa_customer.id, product_id=shoes.id,
        account_id=shop_account.id, txn=None,
    )
    service = _service_with_fake_client()

    async def _fake_dispatch(db, order, product, phone, payment_type, language=None):
        return "DISPATCHED"

    async def _boom(resend):
        raise AssertionError("must not fresh-close")

    monkeypatch.setattr(service, "_dispatch_payment", _fake_dispatch)
    note = await service._resend_payment_note(
        db=db_session, live_order=order, resend={},
        phone_for_dispatch="255700000136", fallback_phone="255700000136",
        language="en", close_fresh_fn=_boom,
    )
    assert note == "DISPATCHED"


async def test_resend_core_mismatch_closes_fresh(db_session, shop_account, wa_customer, shoes):
    order = _pending_order(
        db_session, customer_id=wa_customer.id, product_id=shoes.id, account_id=shop_account.id,
    )
    service = _service_with_fake_client()
    seen = []

    async def _fake_close(resend):
        seen.append(resend)
        return "FRESH"

    note = await service._resend_payment_note(
        db=db_session, live_order=order,
        resend={"product_name": "Hat", "price": 18000, "payment_type": "full"},
        phone_for_dispatch="255700000136", fallback_phone="255700000136",
        language="en", close_fresh_fn=_fake_close,
    )
    assert note == "FRESH"
    assert seen[0]["product_name"] == "Hat"


async def test_resend_core_refusals_return_none(db_session):
    service = _service_with_fake_client()

    async def _boom(resend):
        raise AssertionError("must not close")

    assert await service._resend_payment_note(
        db=db_session, live_order=None,
        resend={"product_name": "Shoes", "price": 1, "payment_type": "installment"},
        phone_for_dispatch="p", fallback_phone="p", language=None, close_fresh_fn=_boom,
    ) is None
    assert await service._resend_payment_note(
        db=db_session, live_order=None, resend={},
        phone_for_dispatch="p", fallback_phone="p", language=None, close_fresh_fn=_boom,
    ) is None
    assert await service._resend_payment_note(
        db=db_session, live_order=None,
        resend={"product_name": "Shoes", "price": 1, "payment_type": "full"},
        phone_for_dispatch=None, fallback_phone=None, language=None, close_fresh_fn=_boom,
    ) is None


# --- platform pitch on Instagram ---------------------------------------------

def _platform_prospect(db_session, ig_platform_account, tag):
    customer = InstagramCustomer(
        ig_scoped_id=f"igsid_prospect_{tag}", username=f"prospect_{tag}",
        account_id=ig_platform_account.id,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = InstagramConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return customer, context


async def test_platform_ig_welcome_first_then_pitch(
    db_session, ig_platform_account, entry_plan,
):
    customer, context = _platform_prospect(db_session, ig_platform_account, "w")
    captured = {}
    service = _service_with_fake_client(captured=captured, reply_text="Habari!")
    await service.generate_dm_response(
        "mambo",
        db=db_session, account=ig_platform_account, customer=customer, context=context,
        conversation_history=[],
    )
    assert "Parity Entry Plan" in captured["system"]
    assert "1. Welcome Message:" in captured["system"]

    db_session.add(InstagramDirectMessage(
        account_id=ig_platform_account.id, customer_id=customer.id,
        direction="outbound", body="Habari!",
    ))
    db_session.commit()
    captured2 = {}
    service2 = _service_with_fake_client(captured=captured2, reply_text="Sawa.")
    await service2.generate_dm_response(
        "sawa, niambie zaidi",
        db=db_session, account=ig_platform_account, customer=customer, context=context,
        conversation_history=[],
    )
    assert "CLOSING MECHANICS" in captured2["system"]
    assert "1. Welcome Message:" not in captured2["system"]
    assert "never offer installments" in captured2["system"]


async def test_platform_ig_close_creates_merchant_and_subscription(
    db_session, ig_platform_account, entry_plan, monkeypatch,
):
    customer, context = _platform_prospect(db_session, ig_platform_account, "c")
    # Past the welcome turn, so the pitch prompt (with close mechanics) is used.
    db_session.add(InstagramDirectMessage(
        account_id=ig_platform_account.id, customer_id=customer.id,
        direction="outbound", body="Habari!",
    ))
    db_session.commit()
    captured = {}
    service = _service_with_fake_client(
        captured=captured,
        reply_text="Asante! [AGREEMENT_REACHED: product=Parity Entry Plan price=100000 payment=full phone=255700000888]",
    )

    async def _fake_dispatch(db, order, product, phone, payment_type, language=None):
        return "PAY-NOTE"

    monkeypatch.setattr(service, "_dispatch_payment", _fake_dispatch)
    await service.generate_dm_response(
        "ndiyo, nataka kujiunga",
        db=db_session, account=ig_platform_account, customer=customer, context=context,
        conversation_history=[],
    )
    assert "never offer installments" in captured["system"]
    merchant = db_session.query(User).filter(User.username == "255700000888").one()
    assert merchant.id is not None
    order = db_session.query(Order).filter(
        Order.instagram_account_id == ig_platform_account.id
    ).one()
    assert order.status == OrderStatus.PENDING
    sub = db_session.query(ProductSubscription).filter(
        ProductSubscription.product_id == entry_plan.id
    ).one()
    assert sub.status == ProductSubscriptionStatus.PENDING_PAYMENT
    assert sub.instagram_customer_id == customer.id


async def test_platform_ig_handoff_alerts_owner(
    db_session, ig_platform_account, entry_plan, monkeypatch,
):
    # #140 reversed the #136 mirroring (platform handoffs were silent):
    # a prospect needing a human on Taskflow's own account is the
    # highest-value notification in the system.
    customer, context = _platform_prospect(db_session, ig_platform_account, "h")
    alerts = []

    async def _recorder(*a, **kw):
        alerts.append(1)
        return {}

    monkeypatch.setattr(LLMService, "_send_platform_alert_to_merchant", _recorder)
    service = _service_with_fake_client(reply_text="Naelewa. [HUMAN_HANDOFF]")
    await service.generate_dm_response(
        "nataka kuongea na mtu",
        db=db_session, account=ig_platform_account, customer=customer, context=context,
        conversation_history=[],
    )
    db_session.refresh(context)
    assert context.state == ConversationState.HUMAN_HANDOFF
    assert alerts == [1]
    event = db_session.query(AmaraEvent).filter(AmaraEvent.kind == AmaraEventKind.HANDOFF).one()
    assert event.channel == "instagram_dm"
