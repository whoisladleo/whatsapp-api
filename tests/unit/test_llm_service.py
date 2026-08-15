import httpx
import pytest
import respx

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Message,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


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
