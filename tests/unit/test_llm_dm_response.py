import json

import anthropic

import httpx
import pytest
import respx

from app.db.models import (
    ConversationState,
    Customer,
    Installment,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService, LLMUnavailable, _holding_reply, _outage_reply

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
INSTALLMENT_URL = "https://pay.tunzaa.co.tz/installments/create"
MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


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


def _service_with_fake_client(handler=None, captured=None, reply_text="It's 45000 TZS."):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse(reply_text)

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    """These tests stub an Anthropic-shaped client, so pin the provider they
    exercise to Claude regardless of ambient LLM_PROVIDER."""
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000400")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_dm_1",
        access_token="ig-tok",
        username="shopowner",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_1", username="commenter1", account_id=ig_account.id)
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
def whatsapp_account(db_session, merchant):
    acct = WhatsAppAccount(
        name="Merchant Shop", phone_number_id="MPHONE1", access_token="wa-tok",
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def shoes(db_session, merchant):
    p = Product(
        slug="ig-dm-shoes", name="Shoes", max_price=45000, min_price=35000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


async def test_generate_dm_response_returns_none_without_client(ig_context):
    service = LLMService(api_key=None)
    result = await service.generate_dm_response(
        "how much?", db=None, account=None, customer=None, context=ig_context, conversation_history=[]
    )
    assert result is None


async def test_generate_dm_response_success(db_session, ig_account, ig_customer, ig_context, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    result = await service.generate_dm_response(
        "how much for the shoes?",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
        account_name="Shoe Shop",
    )

    assert result == "It's 45000 TZS."
    assert "Shoe Shop" in captured["system"]
    assert "Shoes: Max Price TZS 45,000" in captured["system"]
    assert "how much for the shoes?" in captured["messages"][0]["content"]
    # Regression: this exact path (Instagram DM) was observed live quoting
    # a TZS price as USD ("$100,000") — see llm.py's currency RULES.
    assert "Tanzanian Shillings" in captured["system"]
    assert '"$"' in captured["system"]


async def test_generate_dm_response_includes_conversation_history(db_session, ig_account, ig_customer, ig_context, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "and the red ones?",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[
            {"direction": "inbound", "body": "hi, how much for shoes?"},
            {"direction": "outbound", "body": "45000 TZS!"},
        ],
    )

    prompt = captured["messages"][0]["content"]
    assert "Customer: hi, how much for shoes?" in prompt
    assert "Amara: 45000 TZS!" in prompt
    assert "and the red ones?" in prompt


async def test_generate_dm_response_includes_product_description_in_catalog(db_session, ig_account, ig_customer, ig_context):
    """
    Regression test: the catalog string used to only include name/price/
    stock, so the model had no real product details for anything that
    wasn't self-explanatory from its name (e.g. a service/subscription) and
    would invent plausible-sounding but false ones instead.
    """
    p = Product(
        slug="ig-dm-subscription", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=ig_account.user_id,
        description="AI sales assistant for WhatsApp & Instagram — 0 TZS upfront, 100,000 TZS/month.",
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "what is this?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "AI sales assistant for WhatsApp & Instagram" in captured["system"]


async def test_generate_dm_response_catalog_marks_full_payment_only_product(db_session, ig_account, ig_customer, ig_context):
    p = Product(
        slug="ig-dm-cash-only", name="Cash Only Item", max_price=20000, min_price=15000,
        quantity=5, merchant_id=ig_account.user_id, allow_installments=False,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "what do you sell?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "Cash Only Item" in captured["system"]
    assert "(full payment only — installments not offered)" in captured["system"]


async def test_generate_dm_response_catalog_marks_zero_quantity_product_out_of_stock(db_session, ig_account, ig_customer, ig_context):
    p = Product(
        slug="ig-dm-gone", name="Gone Item", max_price=20000, min_price=15000,
        quantity=0, merchant_id=ig_account.user_id,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "do you have Gone Item?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    # Still listed (so "do you have X?" gets a truthful answer), but marked.
    line = next(l for l in captured["system"].splitlines() if "Gone Item" in l)
    assert "Stock: 0" in line
    assert "(OUT OF STOCK)" in line


async def test_generate_dm_response_catalog_marks_inactive_product_out_of_stock(db_session, ig_account, ig_customer, ig_context):
    p = Product(
        slug="ig-dm-retired", name="Retired Item", max_price=20000, min_price=15000,
        quantity=5, merchant_id=ig_account.user_id, is_active=False,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "do you have Retired Item?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    line = next(l for l in captured["system"].splitlines() if "Retired Item" in l)
    assert "(OUT OF STOCK)" in line


async def test_generate_dm_response_catalog_in_stock_line_wording_unchanged_and_states_stock_rule(
    db_session, ig_account, ig_customer, ig_context, shoes
):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "what do you sell?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    line = next(l for l in captured["system"].splitlines() if l.startswith("- Shoes:"))
    assert line == "- Shoes: Max Price TZS 45,000, Lowest Price You May Offer TZS 38,500, Stock: 3"
    # The out-of-stock RULES line is unconditional — present even when
    # nothing is out of stock (#53).
    assert "never offer, quote, or agree a price on a product the catalog marks OUT OF STOCK" in captured["system"]
    assert "suggest an in-stock alternative" in captured["system"]


async def test_generate_dm_response_no_products_says_so_in_catalog(db_session, ig_account, ig_customer, ig_context):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "anything for sale?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "No products currently available." in captured["system"]


async def test_generate_dm_response_includes_knowledge_content(db_session, ig_account, ig_customer, ig_context):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "when do you ship?",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
        knowledge_content="We ship within 2 days.",
    )

    assert "We ship within 2 days." in captured["system"]


async def test_generate_dm_response_handoff_marker_sets_context_state(db_session, ig_account, ig_customer, ig_context):
    service = _service_with_fake_client(
        reply_text="Let me get a teammate to help with that. [HUMAN_HANDOFF]"
    )

    result = await service.generate_dm_response(
        "this is unreasonable", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "[HUMAN_HANDOFF]" not in result
    assert result == "Let me get a teammate to help with that."
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF
    assert ig_context.handoff_at is not None


async def test_generate_dm_response_handoff_only_marker_uses_fallback_text(db_session, ig_account, ig_customer, ig_context):
    service = _service_with_fake_client(reply_text="[HUMAN_HANDOFF]")

    result = await service.generate_dm_response(
        "??", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert result
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


@respx.mock
async def test_generate_dm_response_handoff_notifies_merchant_via_whatsapp(
    db_session, ig_account, ig_customer, ig_context, whatsapp_account
):
    """
    Instagram has no API to message the account owner directly — only
    customers who've messaged the connected account — so a handoff must be
    surfaced cross-channel, via WhatsApp (the platform's own default
    credentials, sent to the merchant's personal number). whatsapp_account
    here is unrelated to how the alert gets sent — it only confirms the
    alert still reaches the right recipient (merchant.username) even when
    they happen to also have a business WhatsAppAccount connected.
    """
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.handoff"}]})
    )
    service = _service_with_fake_client(
        reply_text="Let me get a teammate to help with that. [HUMAN_HANDOFF]"
    )

    await service.generate_dm_response(
        "this is unreasonable", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["to"] == whatsapp_account.owner.username
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "handoff_notification"
    header_params = sent["template"]["components"][0]["parameters"]
    body_params = sent["template"]["components"][1]["parameters"]
    assert {"type": "text", "parameter_name": "customer", "text": "@commenter1"} in header_params
    assert {"type": "text", "parameter_name": "customer", "text": "@commenter1"} in body_params
    # The alert must quote what the *customer* said, not Amara's own reply
    # — that's what the merchant actually has to respond to.
    assert {"type": "text", "parameter_name": "message", "text": "this is unreasonable"} in body_params
    sent_body = route.calls.last.request.content.decode()
    assert "Let me get a teammate to help with that." not in sent_body


@respx.mock
async def test_generate_dm_response_handoff_sent_without_a_whatsapp_account_row(
    db_session, ig_account, ig_customer, ig_context
):
    """
    Regression test for a real production gap: alerts used to require
    merchant.accounts (a connected business WhatsAppAccount) and silently
    no-op'd when it didn't exist. The alert is sent via the platform's own
    default WhatsApp credentials instead — the merchant's personal number
    is how they talk to Amara/Taskflow, independent of whether they've
    connected a business WhatsAppAccount at all (no fixture here has one).
    """
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.handoff"}]})
    )
    service = _service_with_fake_client(reply_text="[HUMAN_HANDOFF]")

    result = await service.generate_dm_response(
        "??", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert result
    assert route.called


async def test_generate_dm_response_handoff_no_merchant_username_does_not_crash(
    db_session, ig_account, ig_customer, ig_context
):
    """Best-effort/silent if the merchant genuinely has no phone on file
    at all (neither username nor phone_number) — nothing to send to."""
    ig_account.owner.username = None
    db_session.commit()

    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.handoff"}]})
    )
    service = _service_with_fake_client(reply_text="[HUMAN_HANDOFF]")

    result = await service.generate_dm_response(
        "??", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert result
    assert not route.called


async def test_generate_dm_response_client_exception_hands_off_with_outage_reply(db_session, ig_account, ig_customer, ig_context):
    async def handler(**kwargs):
        raise RuntimeError("API down")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_dm_response(
        "how much?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    # Used to return None, so the buyer got silence (#38).
    assert result == _outage_reply(None)
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_generate_dm_response_transient_error_raises_when_retrying(db_session, ig_account, ig_customer, ig_context):
    async def handler(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    service = _service_with_fake_client(handler=handler)

    with pytest.raises(LLMUnavailable):
        await service.generate_dm_response(
            "how much?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context,
            conversation_history=[], retry_on_outage=True,
        )
    db_session.refresh(ig_context)
    assert ig_context.state != ConversationState.HUMAN_HANDOFF


# --- AGREEMENT_REACHED (deal closing) -----------------------------------------


@respx.mock
async def test_agreement_with_phone_creates_instagram_order_and_dispatches(
    db_session, ig_account, ig_customer, ig_context, shoes
):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_IG_1"})
    )

    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130099]"
    )

    result = await service.generate_dm_response(
        "I'll take them, full payment, my number is 255711130099",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "AGREEMENT_REACHED" not in result
    assert "sent to your phone" in result
    assert payment_route.called
    # Regression: this used to unconditionally tell the customer to use
    # "WhatsApp's location-sharing feature" even over an Instagram DM —
    # misleading, since that channel has no equivalent we actually process
    # (InstagramWebhookParser never extracts a location, only message.text).
    assert "WhatsApp" not in result
    assert "delivery address" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order is not None
    assert order.instagram_account_id == ig_account.id
    assert order.account_id is None
    assert order.payment_type == PaymentType.FULL
    assert order.status == OrderStatus.PENDING
    assert order.tunzaa_transaction_id == "MOCK_TXN_IG_1"

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711130099").first()
    assert customer is not None
    assert order.customer_id == customer.id

    db_session.refresh(shoes)
    assert shoes.quantity == 2


@respx.mock
async def test_agreement_for_subscription_product_links_instagram_customer(
    db_session, ig_account, ig_customer, ig_context
):
    subscription_product = Product(
        slug="ig-taskflow-sub", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=ig_account.user_id, is_subscription=True,
    )
    db_session.add(subscription_product)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SUB_IG_1"})
    )

    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Taskflow OS Subscription price=100000 payment=full phone=255711130093]"
    )

    await service.generate_dm_response(
        "sign me up, 255711130093",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    order = db_session.query(Order).filter(Order.agreed_price == 100000).first()
    assert order is not None

    from app.db.models import ProductSubscription, ProductSubscriptionStatus

    subscription = db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    assert subscription is not None
    assert subscription.instagram_customer_id == ig_customer.id
    assert subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


@respx.mock
async def test_agreement_installment_creates_instagram_order_and_installments(
    db_session, ig_account, ig_customer, ig_context, shoes
):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "customer": {"customer_id": 1234, "first_name": "John"},
                "plan": {"plan_id": 2002, "status": "active", "total_amount": "45000.00"},
                "installments": [
                    {"installment_id": 20000, "installment_number": 1, "amount": "22500.00", "due_date": "2026-08-16", "status": "PENDING"},
                    {"installment_id": 20001, "installment_number": 2, "amount": "22500.00", "due_date": "2026-11-14", "status": "PENDING"},
                ],
            },
        )
    )

    service = _service_with_fake_client(
        reply_text="Sawa! [AGREEMENT_REACHED: product=Shoes price=45000 payment=installment phone=255711130098]"
    )

    result = await service.generate_dm_response(
        "installments please, 255711130098",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "installment plan has been created" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order.instagram_account_id == ig_account.id
    assert order.payment_type == PaymentType.INSTALLMENT
    assert order.tunzaa_plan_id == "2002"

    installments = db_session.query(Installment).filter(Installment.order_id == order.id).all()
    assert len(installments) == 2


async def test_agreement_without_phone_is_ignored(db_session, ig_account, ig_customer, ig_context, shoes):
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
    )

    result = await service.generate_dm_response(
        "I'll take it", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3


async def test_agreement_price_below_min_ignored(db_session, ig_account, ig_customer, ig_context, shoes):
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=1000 payment=full phone=255711130097]"
    )

    result = await service.generate_dm_response(
        "1000 only", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0
    # The model's "Deal!" must not stand with no order behind it (#37).
    assert result == _holding_reply(None)
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_agreement_unknown_product_ignored(db_session, ig_account, ig_customer, ig_context, shoes):
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Nonexistent price=45000 payment=full phone=255711130096]"
    )

    result = await service.generate_dm_response(
        "sure", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_out_of_stock_ignored(db_session, ig_account, ig_customer, ig_context, shoes):
    shoes.quantity = 0
    db_session.commit()

    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130095]"
    )

    result = await service.generate_dm_response(
        "sure", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_close_deal_exception_hands_off_with_holding_reply(db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch):
    service = _service_with_fake_client(
        reply_text="Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130094]"
    )

    async def raising_close(*a, **kw):
        raise RuntimeError("tunzaa exploded")

    monkeypatch.setattr(service, "_close_instagram_deal", raising_close)

    result = await service.generate_dm_response(
        "sure", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert result == _holding_reply(None)
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_dm_catalog_never_exposes_real_floor(db_session, ig_account, ig_customer, ig_context, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "last price?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "Lowest Price You May Offer TZS 38,500" in captured["system"]
    assert "35,000" not in captured["system"]
    assert "SECRET FLOOR" not in captured["system"]


# --- Usage ledger (#41) ------------------------------------------------------------


async def test_dm_quote_is_stripped_and_recorded(db_session, ig_account, ig_customer, ig_context, shoes):
    from app.db.models import AmaraEvent, AmaraEventKind

    service = _service_with_fake_client(reply_text="I can do TZS 42,000. [QUOTE: product=Shoes price=42000]")

    result = await service.generate_dm_response(
        "how much?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert result == "I can do TZS 42,000."
    quote = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.QUOTE).one()
    assert (quote.product_name, quote.amount, quote.channel) == ("Shoes", 42000, "instagram_dm")


async def test_dm_quote_marker_never_leaks_through_handoff(db_session, ig_account, ig_customer, ig_context, shoes):
    from app.db.models import AmaraEvent, AmaraEventKind

    service = _service_with_fake_client(
        reply_text="Let me check with the shop. [QUOTE: product=Shoes price=42000] [HUMAN_HANDOFF]"
    )

    result = await service.generate_dm_response(
        "hmm", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "QUOTE" not in result and "HUMAN_HANDOFF" not in result
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.HANDOFF).count() == 1
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.QUOTE).count() == 0


async def test_dm_prompt_asks_for_quote_marker(db_session, ig_account, ig_customer, ig_context, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "price?", db=db_session, account=ig_account, customer=ig_customer, context=ig_context, conversation_history=[]
    )

    assert "[QUOTE: product=<product name> price=<price>]" in captured["system"]
