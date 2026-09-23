import json

import httpx
import pytest
import respx

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Installment,
    Message,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)
from app.services.llm import (
    LLMService,
    _parse_and_strip_agreement,
    _parse_and_strip_language,
    _parse_and_strip_quote,
    _truncate_for_alert,
)

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
INSTALLMENT_URL = "https://pay.tunzaa.co.tz/installments/create"


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


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000200")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="LLM_PHONE",
        access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def shoes(db_session, merchant):
    p = Product(
        slug="deal-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def platform_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PLATFORM_PHONE",
        access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


async def test_no_client_returns_unavailable_message():
    service = LLMService(api_key=None)

    result = await service.generate_response(message_body="hi")

    assert result == "AI service is currently unavailable."


async def test_human_handoff_state_returns_none(db_session, shop_account):
    customer = Customer(phone_number="255711110000")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    db_session.add(context)
    db_session.commit()

    service = _service_with_fake_client()

    result = await service.generate_response(
        message_body="hello",
        from_phone="255711110000",
        db=db_session,
        account=shop_account,
    )

    assert result is None


async def test_creates_customer_and_context_on_first_contact(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    result = await service.generate_response(
        message_body="hello",
        from_phone="255711119999",
        db=db_session,
        account=shop_account,
    )

    assert result == "Sure, here is some info."
    customer = db_session.query(Customer).filter(Customer.phone_number == "255711119999").first()
    assert customer is not None
    context = db_session.query(ConversationContext).filter(
        ConversationContext.customer_id == customer.id
    ).first()
    assert context is not None
    assert context.state == ConversationState.NEGOTIATING


# --- Customer.language classification -----------------------------------


async def test_prompts_for_language_marker_when_customer_language_unset(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="hello", from_phone="255711130100", db=db_session, account=shop_account,
    )

    assert "[LANGUAGE: en]" in captured["system"]


async def test_does_not_prompt_for_language_marker_once_already_classified(db_session, shop_account):
    customer = Customer(phone_number="255711130101", language="sw")
    db_session.add(customer)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="habari", from_phone="255711130101", db=db_session, account=shop_account,
    )

    assert "[LANGUAGE:" not in captured["system"]


async def test_language_marker_in_reply_is_persisted_and_stripped(db_session, shop_account):
    async def handler(**kwargs):
        return _FakeResponse("Karibu Taskflow OS! [LANGUAGE: sw]")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="Habari, unauzaje?", from_phone="255711130102", db=db_session, account=shop_account,
    )

    assert result == "Karibu Taskflow OS!"
    assert "[LANGUAGE:" not in result
    customer = db_session.query(Customer).filter(Customer.phone_number == "255711130102").first()
    assert customer.language == "sw"


async def test_language_marker_does_not_overwrite_already_classified_language(db_session, shop_account):
    customer = Customer(phone_number="255711130103", language="en")
    db_session.add(customer)
    db_session.commit()

    async def handler(**kwargs):
        # Model shouldn't be asked for this once classified, but confirm
        # the guard holds even if one somehow slips through.
        return _FakeResponse("Hi there! [LANGUAGE: sw]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="hi", from_phone="255711130103", db=db_session, account=shop_account,
    )

    db_session.refresh(customer)
    assert customer.language == "en"


async def test_extracts_text_when_thinking_block_precedes_it(db_session, shop_account):
    """
    claude-sonnet-5 sometimes returns an extended-thinking block before the
    text block (confirmed live, non-deterministic for the same prompt) —
    response.content[0] isn't reliably the reply, so the extraction must
    filter for the actual text block(s) rather than blindly index [0].
    """
    async def thinking_then_text_handler(**kwargs):
        return _FakeResponse("The real reply.", thinking_first=True)

    service = _service_with_fake_client(handler=thinking_then_text_handler)

    result = await service.generate_response(
        message_body="hello",
        from_phone="255711118888",
        db=db_session,
        account=shop_account,
    )

    assert result == "The real reply."


async def test_customer_lookup_normalizes_phone_across_formats(db_session, shop_account):
    """
    A customer messaging with a leading-0 number should resolve to the same
    Customer row as one already stored in 255-country-code form — this is
    the normalize_phone fix applied to the Customer lookup/creation point.
    """
    existing = Customer(phone_number="255722223333")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    service = _service_with_fake_client()

    await service.generate_response(
        message_body="hello again",
        from_phone="0722223333",
        db=db_session,
        account=shop_account,
    )

    assert db_session.query(Customer).count() == 1
    assert db_session.query(Customer).filter(Customer.phone_number == "255722223333").one().id == existing.id


async def test_platform_merchant_user_lookup_normalizes_phone_across_formats(
    db_session, platform_account
):
    existing = User(username="255733334444")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    service = _service_with_fake_client()

    await service.generate_response(
        message_body="hi",
        from_phone="0733334444",
        db=db_session,
        account=platform_account,
    )

    # No new merchant_user row created for a differently-formatted version
    # of the same existing username.
    assert db_session.query(User).filter(User.username == "255733334444").count() == 1


async def test_regular_account_catalog_uses_account_owner_products(db_session, shop_account, merchant):
    db_session.add(
        Product(slug="shoes", name="Shoes", max_price=50000, min_price=40000, quantity=3, merchant_id=merchant.id)
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111111",
        db=db_session,
        account=shop_account,
    )

    instruction = captured["system"]
    assert "Shoes" in instruction
    assert "Available Products" in instruction
    assert "Test Shop" in instruction
    # Regression: catalog prices had no currency label at all — observed
    # live quoting a TZS price as USD ("$100,000") to a real customer.
    assert "TZS 50,000" in instruction
    assert "Tanzanian Shillings" in instruction
    assert '"$"' in instruction


async def test_regular_account_catalog_includes_product_description(db_session, shop_account, merchant):
    """
    Regression test: the catalog string used to only include name/price/
    stock, so the model had no real product details for anything that
    wasn't self-explanatory from its name (e.g. a service/subscription) and
    would invent plausible-sounding but false ones instead.
    """
    db_session.add(
        Product(
            slug="subscription", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
            quantity=999999, merchant_id=merchant.id,
            description="AI sales assistant for WhatsApp & Instagram — 0 TZS upfront, 100,000 TZS/month.",
        )
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what is this?",
        from_phone="255711111113",
        db=db_session,
        account=shop_account,
    )

    assert "AI sales assistant for WhatsApp & Instagram" in captured["system"]


async def test_regular_account_no_products_shows_placeholder(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111112",
        db=db_session,
        account=shop_account,
    )

    assert "No products currently available." in captured["system"]


async def test_platform_account_first_message_sends_exact_welcome(db_session, platform_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="hi",
        from_phone="255722220000",
        db=db_session,
        account=platform_account,
    )

    instruction = captured["system"]
    assert "Welcome Message" in instruction
    assert "Hi, I'm *Amara*" in instruction

    # The merchant (person texting the platform account) is created as a User.
    merchant_user = db_session.query(User).filter(User.username == "255722220000").first()
    assert merchant_user is not None


async def test_platform_account_subsequent_message_uses_assistance_rules(db_session, platform_account, merchant):
    # An existing outbound message to this phone means it's not the first contact.
    db_session.add(
        Message(
            to_phone="255722220001",
            type="text",
            body="Welcome!",
            direction="outbound",
            status="sent",
            account_id=platform_account.id,
        )
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="how does pricing work?",
        from_phone="255722220001",
        db=db_session,
        account=platform_account,
    )

    instruction = captured["system"]
    assert "Welcome Message" not in instruction
    assert "You CANNOT add products" in instruction
    # Regression: the platform pitch already stated "100,000 TZS" but had
    # no explicit guardrail against also saying "$"/USD — observed live
    # quoting this exact price as "$100,000" to a real prospect.
    assert "Tanzanian Shillings" in instruction
    assert '"$"' in instruction


async def test_knowledge_content_and_system_prompt_included(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="hi",
        from_phone="255711113333",
        db=db_session,
        account=shop_account,
        knowledge_content="Ships in 2 days.",
        system_prompt="Always mention free returns.",
    )

    instruction = captured["system"]
    assert "Ships in 2 days." in instruction
    assert "Always mention free returns." in instruction


def test_get_knowledge_base_reads_existing_file(monkeypatch):
    service = LLMService(api_key=None)
    monkeypatch.setattr("app.services.llm.os.path.exists", lambda path: True)

    import builtins
    from unittest.mock import mock_open

    monkeypatch.setattr(builtins, "open", mock_open(read_data="# Some KB content"))

    result = service._get_knowledge_base("some-slug")

    assert result == "# Some KB content"


def test_get_knowledge_base_missing_file_returns_empty(monkeypatch):
    service = LLMService(api_key=None)
    monkeypatch.setattr("app.services.llm.os.path.exists", lambda path: False)

    result = service._get_knowledge_base("does-not-exist")

    assert result == ""


async def test_falls_back_to_knowledge_base_file_when_no_content_passed(db_session, shop_account, monkeypatch):
    monkeypatch.setattr("app.services.llm.os.path.exists", lambda path: True)
    import builtins
    from unittest.mock import mock_open

    monkeypatch.setattr(builtins, "open", mock_open(read_data="File-based KB text"))

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="hi",
        from_phone="255711114444",
        db=db_session,
        account=shop_account,
        product_slug="shoes-kb",
    )

    assert "File-based KB text" in captured["system"]


async def test_chat_history_included_and_empty_bodies_skipped(db_session, shop_account):
    db_session.add_all(
        [
            Message(
                from_phone="255711115555", to_phone=None, type="text", body="Hi there",
                direction="inbound", status="sent", account_id=shop_account.id,
            ),
            Message(
                to_phone="255711115555", type="text", body="Hello! How can I help?",
                direction="outbound", status="sent", account_id=shop_account.id,
            ),
            Message(
                to_phone="255711115555", type="image", body=None,
                direction="outbound", status="sent", account_id=shop_account.id,
            ),
        ]
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="follow up",
        from_phone="255711115555",
        db=db_session,
        account=shop_account,
    )

    contents = captured["messages"][0]["content"]
    assert "Customer: Hi there" in contents
    assert "Amara: Hello! How can I help?" in contents


@respx.mock
async def test_human_handoff_trigger_updates_state_and_alerts_merchant(
    db_session, shop_account, merchant, monkeypatch
):
    """
    Alert is sent via the platform's own default WhatsApp credentials
    (same ones send_otp_message uses), not the merchant's own business
    WhatsAppAccount (shop_account here) — that's a separate, customer-
    facing channel the merchant may or may not have connected at all.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "PHONE_NUMBER_ID", "PLATFORM_PHONE_TEST")
    platform_account = WhatsAppAccount(
        name="Taskflow", phone_number_id="PLATFORM_PHONE_TEST", access_token="platform-tok",
        user_id=merchant.id,
    )
    db_session.add(platform_account)
    db_session.commit()

    alert_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    scheduled = []
    from app.tasks.handoff_reminders import send_handoff_reminder
    monkeypatch.setattr(
        send_handoff_reminder, "apply_async", lambda *a, **kw: scheduled.append((a, kw))
    )

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116666",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."
    customer = db_session.query(Customer).filter(Customer.phone_number == "255711116666").first()
    context = db_session.query(ConversationContext).filter(
        ConversationContext.customer_id == customer.id
    ).first()
    assert context.state == ConversationState.HUMAN_HANDOFF
    assert context.handoff_at is not None

    assert alert_route.called
    sent_payload = json.loads(alert_route.calls.last.request.content)
    assert sent_payload["type"] == "template"
    assert sent_payload["template"]["name"] == "handoff_notification"
    header_params = sent_payload["template"]["components"][0]["parameters"]
    body_params = sent_payload["template"]["components"][1]["parameters"]
    assert {"type": "text", "parameter_name": "customer", "text": "255711116666"} in header_params
    assert {"type": "text", "parameter_name": "customer", "text": "255711116666"} in body_params
    # The alert must quote what the *customer* said, not Amara's own reply
    # — that's what the merchant actually has to respond to.
    assert {"type": "text", "parameter_name": "message", "text": "I need a human"} in body_params
    sent_body = alert_route.calls.last.request.content.decode()
    assert "Let me get a human." not in sent_body

    # The alert itself is logged as an outbound Message, unlike before.
    alert_message = (
        db_session.query(Message)
        .filter(Message.to_phone == merchant.username, Message.direction == "outbound")
        .first()
    )
    assert alert_message is not None
    assert alert_message.status == "sent"

    # A follow-up reminder is scheduled, keyed on the conversation context id.
    assert len(scheduled) == 1
    assert scheduled[0][0] == ([str(context.id)],) or scheduled[0][1].get("args") == [str(context.id)]


@respx.mock
async def test_human_handoff_alert_still_sent_without_a_platform_whatsapp_account(
    db_session, shop_account, merchant, monkeypatch
):
    """
    Regression test for a real production gap: no WhatsAppAccount row
    existed anywhere in the database (not the merchant's own business
    account, not the platform's own number either), and the handoff alert
    silently never arrived. The alert must still be SENT via the
    platform's default credentials regardless of whether that number is
    also connected as a real WhatsAppAccount row — only the Message-log
    entry (used for the dashboard's own history view) depends on that row
    existing.
    """
    alert_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)
    monkeypatch.setattr(
        "app.tasks.handoff_reminders.send_handoff_reminder.apply_async", lambda *a, **kw: None
    )

    await service.generate_response(
        message_body="I need a human",
        from_phone="255711116667",
        db=db_session,
        account=shop_account,
    )

    assert alert_route.called
    assert db_session.query(Message).filter(
        Message.to_phone == merchant.username, Message.direction == "outbound"
    ).count() == 0


@respx.mock
async def test_human_handoff_alert_send_failure_is_logged_as_failed(db_session, shop_account, merchant, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PHONE_NUMBER_ID", "PLATFORM_PHONE_TEST")
    db_session.add(WhatsAppAccount(
        name="Taskflow", phone_number_id="PLATFORM_PHONE_TEST", access_token="platform-tok",
        user_id=merchant.id,
    ))
    db_session.commit()

    respx.post(url__regex=MESSAGES_URL_RE).mock(return_value=httpx.Response(500))

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116667",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."
    alert_message = (
        db_session.query(Message)
        .filter(Message.to_phone == merchant.username, Message.direction == "outbound")
        .first()
    )
    assert alert_message is not None
    assert alert_message.status in ("sent", "failed")


@respx.mock
async def test_human_handoff_with_empty_remaining_text_uses_default_message(db_session, shop_account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_only_handler(**kwargs):
        return _FakeResponse("[HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_only_handler)

    result = await service.generate_response(
        message_body="human please",
        from_phone="255711117777",
        db=db_session,
        account=shop_account,
    )

    assert result == "I am transferring you to a human agent who can help you further. Please hold."


async def test_platform_account_handoff_does_not_alert_merchant(db_session, platform_account):
    async def handoff_handler(**kwargs):
        return _FakeResponse("[HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    await service.generate_response(
        message_body="human please",
        from_phone="255711118888",
        db=db_session,
        account=platform_account,
    )

    alert_message = (
        db_session.query(Message)
        .filter(Message.direction == "outbound")
        .first()
    )
    assert alert_message is None


@respx.mock
async def test_human_handoff_schedule_failure_does_not_break_reply(
    db_session, shop_account, merchant, monkeypatch
):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    from app.tasks.handoff_reminders import send_handoff_reminder

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(send_handoff_reminder, "apply_async", raising_apply_async)

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116669",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."


async def test_generate_content_exception_returns_fallback_message(db_session, shop_account):
    async def raising_handler(**kwargs):
        raise RuntimeError("Claude is down")

    service = _service_with_fake_client(handler=raising_handler)

    result = await service.generate_response(
        message_body="hi",
        from_phone="255711119000",
        db=db_session,
        account=shop_account,
    )

    assert result == "I'm sorry, I'm having trouble processing your request right now."


# --- _parse_and_strip_agreement (pure function) -----------------------------


def test_parse_agreement_extracts_multi_word_product_and_strips_marker():
    text, agreement = _parse_and_strip_agreement(
        "Great, let's do it! [AGREEMENT_REACHED: product=Running Shoes price=45000 payment=full]"
    )

    assert text == "Great, let's do it!"
    assert agreement == {
        "product_name": "Running Shoes",
        "price": 45000.0,
        "payment_type": "full",
        "phone": None,
    }


def test_parse_agreement_case_insensitive():
    text, agreement = _parse_and_strip_agreement(
        "[agreement_reached: PRODUCT=Shoes PRICE=45000 PAYMENT=INSTALLMENT]"
    )

    assert agreement["payment_type"] == "installment"
    assert agreement["product_name"] == "Shoes"


def test_parse_agreement_extracts_optional_phone_for_instagram_dm_flow():
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130000]"
    )

    assert text == "Deal!"
    assert agreement["phone"] == "255711130000"


def test_parse_agreement_no_marker_present():
    text, agreement = _parse_and_strip_agreement("Just a normal reply.")

    assert text == "Just a normal reply."
    assert agreement is None


def test_parse_agreement_missing_field_not_matched():
    original = "Deal! [AGREEMENT_REACHED: product=Shoes price=45000]"

    text, agreement = _parse_and_strip_agreement(original)

    assert agreement is None
    assert text == original  # regex didn't match at all, nothing stripped


def test_parse_agreement_blank_product_name_returns_none():
    # Two spaces between "product=" and "price=" lets the non-greedy .+?
    # capture a single whitespace character, which strips to empty.
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=  price=45000 payment=full]"
    )

    assert agreement is None
    assert "AGREEMENT_REACHED" not in text


def test_parse_agreement_unparseable_price_returns_none():
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=Shoes price=45.65.23 payment=full]"
    )

    assert agreement is None
    assert "AGREEMENT_REACHED" not in text  # still stripped even though unusable


# --- Deal closing: full end-to-end via generate_response --------------------


@respx.mock
async def test_agreement_full_payment_creates_order_and_dispatches(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_1"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take the shoes for 45000, full payment",
        from_phone="255711130000",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert "sent to your phone" in result
    assert "location" in result.lower()
    # WhatsApp orders should reference WhatsApp's own location-sharing
    # feature — see the Instagram-side regression test in
    # test_llm_dm_response.py for why this can't be generic/hardcoded.
    assert "WhatsApp's location-sharing feature" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order is not None
    assert order.product_id == shoes.id
    assert order.account_id == shop_account.id
    assert order.quantity == 1
    assert order.payment_type == PaymentType.FULL
    assert order.status == OrderStatus.PENDING
    assert order.reserved_until is not None
    assert order.tunzaa_transaction_id == "MOCK_TXN_1"
    assert payment_route.called

    db_session.refresh(shoes)
    assert shoes.quantity == 2  # reserved at creation time, decremented from 3

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711130000").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.last_offer == 45000


@respx.mock
async def test_agreement_for_subscription_product_creates_product_subscription(db_session, shop_account, merchant):
    subscription_product = Product(
        slug="taskflow-sub", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(subscription_product)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SUB_1"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Taskflow OS Subscription price=100000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="sign me up",
        from_phone="255711130050",
        db=db_session,
        account=shop_account,
    )

    order = db_session.query(Order).filter(Order.agreed_price == 100000).first()
    assert order is not None

    subscription = db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    assert subscription is not None
    assert subscription.product_id == subscription_product.id
    assert subscription.customer_id == order.customer_id
    assert subscription.instagram_customer_id is None
    assert subscription.monthly_amount == 100000
    assert subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


@respx.mock
async def test_repeated_subscription_agreement_reuses_pending_subscription_row(db_session, shop_account, merchant):
    """
    Regression test: a customer who negotiates a subscription product twice
    before ever completing payment (e.g. abandons the first Tunzaa prompt
    and starts over) must not end up with two orphaned PENDING_PAYMENT
    ProductSubscription rows for the same customer+product — see
    product_subscription.get_or_create_pending_subscription.
    """
    subscription_product = Product(
        slug="taskflow-sub-2", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(subscription_product)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SUB_2"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Taskflow OS Subscription price=100000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    for _ in range(2):
        await service.generate_response(
            message_body="sign me up",
            from_phone="255711130060",
            db=db_session,
            account=shop_account,
        )

    orders = db_session.query(Order).filter(Order.agreed_price == 100000, Order.product_id == subscription_product.id).all()
    assert len(orders) == 2

    subscriptions = (
        db_session.query(ProductSubscription).filter(ProductSubscription.product_id == subscription_product.id).all()
    )
    assert len(subscriptions) == 1
    assert subscriptions[0].order_id == orders[-1].id


@respx.mock
async def test_agreement_for_regular_product_does_not_create_product_subscription(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_2"})
    )

    async def handler(**kwargs):
        return _FakeResponse("Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="I'll take the shoes",
        from_phone="255711130051",
        db=db_session,
        account=shop_account,
    )

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order is not None
    assert db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).count() == 0


@respx.mock
async def test_agreement_installment_creates_order_and_installment_rows(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "customer": {"customer_id": 1234, "first_name": "John"},
                "plan": {"plan_id": 1001, "status": "active", "total_amount": "45000.00"},
                "installments": [
                    {"installment_id": 10000, "installment_number": 1, "amount": "22500.00", "due_date": "2026-08-16", "status": "PENDING"},
                    {"installment_id": 10001, "installment_number": 2, "amount": "22500.00", "due_date": "2026-11-14", "status": "PENDING"},
                ],
            },
        )
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Sawa! [AGREEMENT_REACHED: product=Shoes price=45000 payment=installment]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll pay in installments",
        from_phone="255711130001",
        db=db_session,
        account=shop_account,
    )

    assert "installment plan has been created" in result
    assert "location" in result.lower()

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order.payment_type == PaymentType.INSTALLMENT
    assert order.tunzaa_plan_id == "1001"

    installments = db_session.query(Installment).filter(Installment.order_id == order.id).all()
    assert len(installments) == 2
    assert {i.tunzaa_installment_id for i in installments} == {"10000", "10001"}
    assert all(i.amount_due == 22500.0 for i in installments)


async def test_agreement_price_below_min_ignored(db_session, shop_account, shoes):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=1000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="1000 tzs please",
        from_phone="255711130002",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3  # untouched


async def test_agreement_unknown_product_ignored(db_session, shop_account, shoes):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Nonexistent Widget price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="widget please",
        from_phone="255711130003",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_out_of_stock_ignored(db_session, shop_account, shoes):
    shoes.quantity = 0
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130004",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_and_handoff_together_handoff_wins(db_session, shop_account, shoes, merchant, monkeypatch):
    async def fake_alert(self, to, name, language_code="en_US", components=None):
        return {"messages": [{"id": "wamid.alert"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", fake_alert)

    async def handler(**kwargs):
        return _FakeResponse(
            "Let me get a human. [HUMAN_HANDOFF] [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="actually let me talk to a person, and I'll take the shoes for 45000 full",
        from_phone="255711130005",
        db=db_session,
        account=shop_account,
    )

    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3


@respx.mock
async def test_agreement_tunzaa_dispatch_failure_releases_reservation(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130006",
        db=db_session,
        account=shop_account,
    )

    assert "went wrong setting up your payment" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order.status == OrderStatus.CANCELLED

    db_session.refresh(shoes)
    assert shoes.quantity == 3  # decremented then released back


async def test_agreement_close_deal_exception_does_not_lose_reply(db_session, shop_account, shoes, monkeypatch):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    async def raising_close_deal(*a, **kw):
        raise RuntimeError("unexpected DB error")

    monkeypatch.setattr(service, "_close_deal", raising_close_deal)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130007",
        db=db_session,
        account=shop_account,
    )

    # The valid AI reply survives even though the order-completion side
    # effect blew up — it's not replaced by the generic Claude-error fallback.
    assert result == "Deal!"
    assert db_session.query(Order).count() == 0


@respx.mock
async def test_agreement_installment_with_missing_due_date_defaults_gracefully(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "plan": {"plan_id": 1001},
                "installments": [
                    {"installment_id": 1, "amount": "45000.00"},  # no due_date key at all
                ],
            },
        )
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Sawa! [AGREEMENT_REACHED: product=Shoes price=45000 payment=installment]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="installments please",
        from_phone="255711130009",
        db=db_session,
        account=shop_account,
    )

    assert "installment plan has been created" in result
    installment = db_session.query(Installment).filter(Installment.tunzaa_installment_id == "1").first()
    assert installment is not None
    assert installment.due_date is not None  # defaulted rather than raising


async def test_agreement_reminder_schedule_failure_does_not_break_order_creation(
    db_session, shop_account, shoes, monkeypatch
):
    from app.tasks.order_reminders import send_soft_nudge

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(send_soft_nudge, "apply_async", raising_apply_async)

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    import respx as respx_module
    with respx_module.mock:
        respx_module.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
        respx_module.post(PAYMENT_URL).mock(
            return_value=httpx.Response(200, json={"transactionID": "MOCK_TXN_2"})
        )
        result = await service.generate_response(
            message_body="I'll take it",
            from_phone="255711130010",
            db=db_session,
            account=shop_account,
        )

    assert "sent to your phone" in result
    assert db_session.query(Order).count() == 1


async def test_agreement_ignored_on_platform_account(db_session, platform_account, merchant):
    db_session.add(
        Product(slug="platform-widget", name="Widget", max_price=100, min_price=50, quantity=5, merchant_id=merchant.id)
    )
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse(
            "Sure! [AGREEMENT_REACHED: product=Widget price=75 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take a widget",
        from_phone="255711130008",
        db=db_session,
        account=platform_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


def test_truncate_for_alert_leaves_short_text_unchanged():
    assert _truncate_for_alert("Do you have this in size 42?") == "Do you have this in size 42?"


def test_truncate_for_alert_truncates_long_text_with_ellipsis():
    long_text = "a" * 250
    result = _truncate_for_alert(long_text, max_chars=200)
    assert len(result) == 201  # 200 chars + the ellipsis character
    assert result.endswith("…")
    assert result.startswith("a" * 200)


def test_parse_and_strip_quote_extracts_and_strips_marker():
    text, quote = _parse_and_strip_quote(
        "I can do TZS 45,000. [QUOTE: product=Shoes price=45000]"
    )
    assert text == "I can do TZS 45,000."
    assert quote == {"product_name": "Shoes", "price": 45000.0}


def test_parse_and_strip_quote_no_marker_returns_none():
    text, quote = _parse_and_strip_quote("Just chatting, no price yet.")
    assert text == "Just chatting, no price yet."
    assert quote is None


def test_parse_and_strip_language_extracts_and_strips_marker():
    text, language = _parse_and_strip_language("Karibu! [LANGUAGE: sw]")
    assert text == "Karibu!"
    assert language == "sw"


def test_parse_and_strip_language_case_insensitive():
    text, language = _parse_and_strip_language("Hello! [language: EN]")
    assert text == "Hello!"
    assert language == "en"


def test_parse_and_strip_language_no_marker_returns_none():
    text, language = _parse_and_strip_language("Just a normal reply.")
    assert text == "Just a normal reply."
    assert language is None


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
