"""Length-cutoff handling (#103).

A model reply stopped for length (Muse finish_reason=length / Claude
stop_reason=max_tokens) must never reach the buyer mid-sentence: providers
raise LLMLengthCutoff, generate_* retries once with a doubled cap, and a
second cut hands off with the holding reply.
"""

import pytest

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import (
    ClaudeProvider,
    LLMService,
    LLMLengthCutoff,
    MuseProvider,
    _holding_reply,
)


class _FakeContentBlock:
    def __init__(self, text=None, type="text"):
        self.type = type
        if text is not None:
            self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text="", stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeContentBlock(text=text)]


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


class _FakeChatMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _FakeChatMessage(content)
        self.finish_reason = finish_reason


class _FakeChatCompletion:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeCompletions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeChat:
    def __init__(self, handler):
        self.completions = _FakeCompletions(handler)


class _FakeOpenAIClient:
    def __init__(self, handler):
        self.chat = _FakeChat(handler)


def _scripted_claude_service(responses):
    """responses: [(text, stop_reason), ...] per call; returns (service, calls)."""
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs)
        text, stop = responses[min(len(calls) - 1, len(responses) - 1)]
        return _FakeAnthropicResponse(text, stop)

    service = LLMService(api_key=None)
    service.client = _FakeAnthropicClient(handler)
    return service, calls


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000950")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_cutoff", access_token="tok", username="cutoffshop",
        ai_enabled=True, user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_cutoff", username="cutoffbuyer", account_id=ig_account.id)
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
def shop_account(db_session, merchant):
    acct = WhatsAppAccount(
        name="Cutoff Shop", phone_number_id="CUTOFF_PHONE", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def cutoff_shoes(db_session, merchant):
    p = Product(
        slug="cutoff-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# --- Provider level: length stops raise, normal stops return -----------------


async def test_muse_provider_raises_on_finish_reason_length():
    provider = MuseProvider(api_key="x")
    async def cut(**kwargs):
        return _FakeChatCompletion("…uithibit", finish_reason="length")

    provider.client = _FakeOpenAIClient(cut)

    with pytest.raises(LLMLengthCutoff):
        await provider.complete(system="s", messages=[], max_tokens=500)


async def test_muse_provider_returns_content_on_stop():
    provider = MuseProvider(api_key="x")
    async def full(**kwargs):
        return _FakeChatCompletion("Karibu!", finish_reason="stop")

    provider.client = _FakeOpenAIClient(full)

    assert await provider.complete(system="s", messages=[], max_tokens=500) == "Karibu!"


async def test_claude_provider_raises_on_stop_reason_max_tokens():
    provider = ClaudeProvider(api_key="x")
    async def cut(**kwargs):
        return _FakeAnthropicResponse("…per", stop_reason="max_tokens")

    provider.client = _FakeAnthropicClient(cut)

    with pytest.raises(LLMLengthCutoff):
        await provider.complete(system="s", messages=[], max_tokens=500)


async def test_claude_provider_returns_text_on_end_turn():
    provider = ClaudeProvider(api_key="x")
    async def full(**kwargs):
        return _FakeAnthropicResponse("Karibu!", stop_reason="end_turn")

    provider.client = _FakeAnthropicClient(full)

    assert await provider.complete(system="s", messages=[], max_tokens=500) == "Karibu!"


# --- Generate level: retry with doubled cap, then holding + handoff ----------


async def test_dm_cutoff_retries_with_doubled_cap(
    db_session, ig_account, ig_customer, ig_context, cutoff_shoes
):
    service, calls = _scripted_claude_service([
        ("…uithibit", "max_tokens"),
        ("Sawa, nitakutumia ombi la malipo!", "end_turn"),
    ])

    result = await service.generate_dm_response(
        "nalipaje", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )

    assert result == "Sawa, nitakutumia ombi la malipo!"
    assert [c["max_tokens"] for c in calls] == [500, 1000]
    db_session.refresh(ig_context)
    assert ig_context.state != ConversationState.HUMAN_HANDOFF


async def test_dm_double_cutoff_hands_off_with_holding(
    db_session, ig_account, ig_customer, ig_context, cutoff_shoes
):
    service, calls = _scripted_claude_service([("…uithibit", "max_tokens")])

    result = await service.generate_dm_response(
        "nalipaje", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )

    assert result == _holding_reply(None)
    assert "uithibit" not in result
    assert [c["max_tokens"] for c in calls] == [500, 1000]
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_wa_cutoff_retries_with_doubled_cap(db_session, shop_account, cutoff_shoes):
    service, calls = _scripted_claude_service([
        ("…per", "max_tokens"),
        ("The price stays at TZS 100,000 per month.", "end_turn"),
    ])

    result = await service.generate_response(
        message_body="best price?", from_phone="255711190020",
        db=db_session, account=shop_account,
    )

    assert result == "The price stays at TZS 100,000 per month."
    assert [c["max_tokens"] for c in calls] == [1024, 2048]


async def test_wa_double_cutoff_hands_off_with_holding(db_session, shop_account, cutoff_shoes):
    service, calls = _scripted_claude_service([("…per", "max_tokens")])

    result = await service.generate_response(
        message_body="best price?", from_phone="255711190021",
        db=db_session, account=shop_account,
    )

    assert result == _holding_reply(None)
    assert "per" != result.strip()
    assert [c["max_tokens"] for c in calls] == [1024, 2048]
    customer = db_session.query(Customer).filter_by(phone_number="255711190021").one()
    context = db_session.query(ConversationContext).filter_by(customer_id=customer.id).one()
    assert context.state == ConversationState.HUMAN_HANDOFF
