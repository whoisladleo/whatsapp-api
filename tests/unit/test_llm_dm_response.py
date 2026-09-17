import pytest

from app.db.models import Product, User
from app.services.llm import LLMService


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


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000400")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


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


async def test_generate_dm_response_returns_none_without_client():
    service = LLMService(api_key=None)
    result = await service.generate_dm_response(
        "how much?", merchant_id=1, db=None, conversation_history=[]
    )
    assert result is None


async def test_generate_dm_response_success(db_session, merchant, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    result = await service.generate_dm_response(
        "how much for the shoes?",
        merchant_id=merchant.id,
        db=db_session,
        conversation_history=[],
        account_name="Shoe Shop",
    )

    assert result == {"reply": "It's 45000 TZS.", "handoff": False}
    assert "Shoe Shop" in captured["system"]
    assert "Shoes: Max Price 45000" in captured["system"]
    assert "how much for the shoes?" in captured["messages"][0]["content"]


async def test_generate_dm_response_includes_conversation_history(db_session, merchant, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "and the red ones?",
        merchant_id=merchant.id,
        db=db_session,
        conversation_history=[
            {"direction": "inbound", "body": "hi, how much for shoes?"},
            {"direction": "outbound", "body": "45000 TZS!"},
        ],
    )

    prompt = captured["messages"][0]["content"]
    assert "Customer: hi, how much for shoes?" in prompt
    assert "Amara: 45000 TZS!" in prompt
    assert "and the red ones?" in prompt


async def test_generate_dm_response_no_products_says_so_in_catalog(db_session, merchant):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "anything for sale?", merchant_id=merchant.id, db=db_session, conversation_history=[]
    )

    assert "No products currently available." in captured["system"]


async def test_generate_dm_response_includes_knowledge_content(db_session, merchant):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_dm_response(
        "when do you ship?",
        merchant_id=merchant.id,
        db=db_session,
        conversation_history=[],
        knowledge_content="We ship within 2 days.",
    )

    assert "We ship within 2 days." in captured["system"]


async def test_generate_dm_response_strips_handoff_marker_and_flags_it(db_session, merchant):
    service = _service_with_fake_client(
        reply_text="Great, a team member will follow up to complete your order! [HUMAN_HANDOFF]"
    )

    result = await service.generate_dm_response(
        "I'll take it, how do I pay?",
        merchant_id=merchant.id,
        db=db_session,
        conversation_history=[],
    )

    assert result["handoff"] is True
    assert "[HUMAN_HANDOFF]" not in result["reply"]
    assert result["reply"] == "Great, a team member will follow up to complete your order!"


async def test_generate_dm_response_handoff_only_marker_uses_fallback_text(db_session, merchant):
    service = _service_with_fake_client(reply_text="[HUMAN_HANDOFF]")

    result = await service.generate_dm_response(
        "this is unreasonable", merchant_id=merchant.id, db=db_session, conversation_history=[]
    )

    assert result["handoff"] is True
    assert result["reply"]


async def test_generate_dm_response_returns_none_on_client_exception(db_session, merchant):
    async def handler(**kwargs):
        raise RuntimeError("API down")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_dm_response(
        "how much?", merchant_id=merchant.id, db=db_session, conversation_history=[]
    )

    assert result is None
