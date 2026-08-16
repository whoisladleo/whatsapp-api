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
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService, _parse_and_strip_agreement

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
INSTALLMENT_URL = "https://pay.tunzaa.co.tz/installments/create"


class _FakeModels:
    def __init__(self, handler):
        self._handler = handler

    async def generate_content(self, model, contents, config):
        return await self._handler(model=model, contents=contents, config=config)


class _FakeAio:
    def __init__(self, handler):
        self.models = _FakeModels(handler)


class _FakeGenAIClient:
    def __init__(self, handler):
        self.aio = _FakeAio(handler)


class _FakeResponse:
    def __init__(self, text):
        self.text = text


def _service_with_fake_client(handler=None, captured=None):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse("Sure, here is some info.")

    service.client = _FakeGenAIClient(handler or default_handler)
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

    instruction = captured["config"].system_instruction
    assert "Shoes" in instruction
    assert "Available Products" in instruction
    assert "Test Shop" in instruction


async def test_regular_account_no_products_shows_placeholder(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111112",
        db=db_session,
        account=shop_account,
    )

    assert "No products currently available." in captured["config"].system_instruction


async def test_platform_account_first_message_sends_exact_welcome(db_session, platform_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="hi",
        from_phone="255722220000",
        db=db_session,
        account=platform_account,
    )

    instruction = captured["config"].system_instruction
    assert "Welcome Message" in instruction
    assert "Habari! My name is *Amara*" in instruction

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

    instruction = captured["config"].system_instruction
    assert "Welcome Message" not in instruction
    assert "You CANNOT add products" in instruction


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

    instruction = captured["config"].system_instruction
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

    assert "File-based KB text" in captured["config"].system_instruction


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

    contents = captured["contents"]
    assert "Customer: Hi there" in contents
    assert "Amara: Hello! How can I help?" in contents


@respx.mock
async def test_human_handoff_trigger_updates_state_and_alerts_merchant(
    db_session, shop_account, merchant, monkeypatch
):
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
    sent_body = alert_route.calls.last.request.content.decode()
    assert "255711116666" in sent_body

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
    assert scheduled[0][0] == ([context.id],) or scheduled[0][1].get("args") == [context.id]


@respx.mock
async def test_human_handoff_alert_send_failure_is_logged_as_failed(db_session, shop_account, merchant):
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
        raise RuntimeError("Gemini is down")

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
    assert agreement == {"product_name": "Running Shoes", "price": 45000.0, "payment_type": "full"}


def test_parse_agreement_case_insensitive():
    text, agreement = _parse_and_strip_agreement(
        "[agreement_reached: PRODUCT=Shoes PRICE=45000 PAYMENT=INSTALLMENT]"
    )

    assert agreement["payment_type"] == "installment"
    assert agreement["product_name"] == "Shoes"


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
    async def fake_alert(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.alert"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_alert)

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
    # effect blew up — it's not replaced by the generic Gemini-error fallback.
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
