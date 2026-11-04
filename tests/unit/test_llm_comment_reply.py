import pytest

from app.db.models import Product, User
from app.services.llm import LLMService, _parse_comment_reply


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


def _service_with_fake_client(handler=None, captured=None):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse(
            "PUBLIC: Thanks for asking! Sent you the details in DMs.\n"
            "PRIVATE: It's 45000 TZS and we have stock ready to go."
        )

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    """These tests stub an Anthropic-shaped client, so pin the provider they
    exercise to Claude regardless of ambient LLM_PROVIDER."""
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000300")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shoes(db_session, merchant):
    p = Product(
        slug="ig-shoes", name="Shoes", max_price=45000, min_price=35000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_parse_comment_reply_extracts_both_lines():
    text = "PUBLIC: Hi!\nPRIVATE: It's 10000 TZS."
    assert _parse_comment_reply(text) == ("Hi!", "It's 10000 TZS.")


def test_parse_comment_reply_returns_none_on_malformed_output():
    assert _parse_comment_reply("Just a plain sentence, no markers.") is None


def test_parse_comment_reply_returns_none_on_empty_line():
    assert _parse_comment_reply("PUBLIC: \nPRIVATE: something") is None


async def test_generate_comment_reply_returns_none_without_client():
    service = LLMService(api_key=None)
    result = await service.generate_comment_reply("how much?", merchant_id=1, db=None)
    assert result is None


async def test_generate_comment_reply_success(db_session, merchant, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    result = await service.generate_comment_reply(
        "how much for the shoes?", merchant_id=merchant.id, db=db_session, account_name="Shoe Shop"
    )

    assert result == {
        "public_reply": "Thanks for asking! Sent you the details in DMs.",
        "private_reply": "It's 45000 TZS and we have stock ready to go.",
    }
    assert "Shoe Shop" in captured["system"]
    assert "how much for the shoes?" in captured["system"]
    assert "Shoes: Price TZS 45,000" in captured["system"]
    # Regression: catalog prices had no currency label at all, and the
    # model was observed live quoting a TZS price as USD ("$100,000")
    # instead — see llm.py's currency RULES additions.
    assert "Tanzanian Shillings" in captured["system"]
    assert '"$"' in captured["system"]
    # Regression (#50): Muse returned empty (reasoning exhausted the 300
    # budget, finish_reason=length) on off-topic comments with no guidance
    # for what to do there — sends nothing. The prompt now tells both
    # models to deflect in-format.
    assert "has nothing to do with the shop" in captured["system"]


async def test_generate_comment_reply_includes_product_description_in_catalog(db_session, merchant):
    """
    Regression test: the catalog string used to only include name/price/
    stock, so the model had no real product details for anything that
    wasn't self-explanatory from its name (e.g. a service/subscription) and
    would invent plausible-sounding but false ones instead.
    """
    p = Product(
        slug="ig-subscription", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id,
        description="AI sales assistant for WhatsApp & Instagram — 0 TZS upfront, 100,000 TZS/month.",
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "what is this?", merchant_id=merchant.id, db=db_session
    )

    assert "AI sales assistant for WhatsApp & Instagram" in captured["system"]


async def test_generate_comment_reply_catalog_marks_zero_quantity_product_out_of_stock(db_session, merchant):
    p = Product(
        slug="ig-gone", name="Gone Item", max_price=20000, min_price=15000,
        quantity=0, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "do you have Gone Item?", merchant_id=merchant.id, db=db_session
    )

    # Still listed (so "do you have X?" gets a truthful answer), but marked.
    # (Anchored: the comment text itself is also quoted in this prompt.)
    line = next(l for l in captured["system"].splitlines() if l.startswith("- Gone Item:"))
    assert "Stock: 0" in line
    assert "(OUT OF STOCK)" in line


async def test_generate_comment_reply_catalog_marks_inactive_product_out_of_stock(db_session, merchant):
    p = Product(
        slug="ig-retired", name="Retired Item", max_price=20000, min_price=15000,
        quantity=5, merchant_id=merchant.id, is_active=False,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "do you have Retired Item?", merchant_id=merchant.id, db=db_session
    )

    line = next(l for l in captured["system"].splitlines() if l.startswith("- Retired Item:"))
    assert "(OUT OF STOCK)" in line


async def test_generate_comment_reply_catalog_subscription_is_available_not_stock_zero(db_session, merchant):
    p = Product(
        slug="ig-plan", name="Entry Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "bei gani kwa mwezi?", merchant_id=merchant.id, db=db_session
    )

    line = next(l for l in captured["system"].splitlines() if l.startswith("- Entry Plan:"))
    assert line == "- Entry Plan: Price TZS 100,000, available now, billed monthly"


async def test_generate_comment_reply_catalog_excludes_drafts(db_session, merchant):
    p = Product(
        slug="ig-draft", name="Untitled import 12345678", max_price=0.0, min_price=0.0,
        quantity=0, merchant_id=merchant.id, is_active=False, is_draft=True,
    )
    db_session.add(p)
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "what do you sell?", merchant_id=merchant.id, db=db_session
    )

    assert "Untitled import" not in captured["system"]


async def test_generate_comment_reply_catalog_in_stock_line_wording_unchanged_and_states_stock_rule(db_session, merchant, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "how much for the shoes?", merchant_id=merchant.id, db=db_session, account_name="Shoe Shop"
    )

    line = next(l for l in captured["system"].splitlines() if l.startswith("- Shoes:"))
    assert line == "- Shoes: Price TZS 45,000, Stock: 3"
    # The out-of-stock instruction is unconditional — present even when
    # nothing is out of stock (#53).
    assert "never offer a product the catalog marks OUT OF STOCK" in captured["system"]
    assert "suggest an in-stock alternative" in captured["system"]


async def test_generate_comment_reply_no_products_says_so_in_catalog(db_session, merchant):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "anything for sale?", merchant_id=merchant.id, db=db_session
    )

    assert "No products currently available." in captured["system"]


async def test_generate_comment_reply_includes_knowledge_content(db_session, merchant):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_comment_reply(
        "when do you ship?",
        merchant_id=merchant.id,
        db=db_session,
        knowledge_content="We ship within 2 days.",
    )

    assert "We ship within 2 days." in captured["system"]


async def test_generate_comment_reply_returns_none_on_malformed_model_output(db_session, merchant):
    async def handler(**kwargs):
        return _FakeResponse("Sorry, I can't help with that.")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session
    )

    assert result is None


async def test_generate_comment_reply_returns_none_on_client_exception(db_session, merchant):
    async def handler(**kwargs):
        raise RuntimeError("API down")

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_comment_reply(
        "how much?", merchant_id=merchant.id, db=db_session
    )

    assert result is None
