import anthropic
import httpx
import pytest

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Message,
    User,
)
from app.services.llm import (
    LLMService,
    LLMUnavailable,
    _outage_reply,
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
    # Regression (#45): the pitch used to promise "zero upfront, auto-deducted
    # from sales", contradicting the site and the actual billing (a flat
    # monthly payment request, no commission).
    assert "*no commission*" in instruction
    assert "Zero Upfront" not in instruction
    assert "auto-deducted" not in instruction


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

    # Any Claude failure that isn't being retried hands off too (#38).
    assert result == _outage_reply(None)
    assert db_session.query(ConversationContext).one().state == ConversationState.HUMAN_HANDOFF


async def test_transient_error_raises_llm_unavailable_when_retrying(db_session, shop_account):
    async def handler(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    service = _service_with_fake_client(handler=handler)

    with pytest.raises(LLMUnavailable):
        await service.generate_response(
            message_body="hi", from_phone="255711119001", db=db_session, account=shop_account,
            retry_on_outage=True,
        )
    assert db_session.query(ConversationContext).one().state != ConversationState.HUMAN_HANDOFF


async def test_non_transient_error_hands_off_even_when_retrying(db_session, shop_account):
    async def handler(**kwargs):
        raise RuntimeError("bug, not an outage")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="hi", from_phone="255711119002", db=db_session, account=shop_account,
        retry_on_outage=True,
    )

    assert result == _outage_reply(None)
