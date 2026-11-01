from app.db.models import (
    Product,
)
from app.services.llm import (
    LLMService,
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


async def test_catalog_marks_full_payment_only_product(db_session, shop_account, merchant):
    db_session.add(
        Product(
            slug="cash-only", name="Cash Only Item", max_price=20000, min_price=15000,
            quantity=5, merchant_id=merchant.id, allow_installments=False,
        )
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111114",
        db=db_session,
        account=shop_account,
    )

    assert "Cash Only Item" in captured["system"]
    assert "(full payment only — installments not offered)" in captured["system"]


async def test_catalog_omits_full_payment_only_note_when_installments_allowed(db_session, shop_account, merchant):
    db_session.add(
        Product(slug="shoes-2", name="Shoes 2", max_price=50000, min_price=40000, quantity=3, merchant_id=merchant.id)
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111115",
        db=db_session,
        account=shop_account,
    )

    assert "(full payment only — installments not offered)" not in captured["system"]


async def test_catalog_marks_zero_quantity_product_out_of_stock(db_session, shop_account, merchant):
    db_session.add(
        Product(slug="gone-item", name="Gone Item", max_price=20000, min_price=15000, quantity=0, merchant_id=merchant.id)
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="do you have Gone Item?",
        from_phone="255711111116",
        db=db_session,
        account=shop_account,
    )

    # Still listed (so "do you have X?" gets a truthful answer), but marked.
    line = next(l for l in captured["system"].splitlines() if "Gone Item" in l)
    assert "Stock: 0" in line
    assert "(OUT OF STOCK)" in line


async def test_catalog_marks_inactive_product_out_of_stock(db_session, shop_account, merchant):
    db_session.add(
        Product(
            slug="retired-item", name="Retired Item", max_price=20000, min_price=15000,
            quantity=5, merchant_id=merchant.id, is_active=False,
        )
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="do you have Retired Item?",
        from_phone="255711111117",
        db=db_session,
        account=shop_account,
    )

    line = next(l for l in captured["system"].splitlines() if "Retired Item" in l)
    assert "(OUT OF STOCK)" in line


async def test_catalog_excludes_drafts_entirely(db_session, shop_account, merchant):
    db_session.add(
        Product(
            slug="draft-item", name="Untitled import 12345678", max_price=0.0, min_price=0.0,
            quantity=0, merchant_id=merchant.id, is_active=False, is_draft=True,
        )
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111118",
        db=db_session,
        account=shop_account,
    )

    # Not listed at all (unlike out-of-stock rows) — the model must not
    # offer, match, or quote a placeholder.
    assert "Untitled import" not in captured["system"]


async def test_catalog_in_stock_line_wording_unchanged_and_states_stock_rule(db_session, shop_account, merchant):
    db_session.add(
        Product(slug="shoes-3", name="Shoes", max_price=50000, min_price=40000, quantity=3, merchant_id=merchant.id)
    )
    db_session.commit()

    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111118",
        db=db_session,
        account=shop_account,
    )

    line = next(l for l in captured["system"].splitlines() if l.startswith("- Shoes:"))
    assert line == "- Shoes: Max Price TZS 50,000, Lowest Price You May Offer TZS 44,000, Stock: 3"
    # The out-of-stock RULES line is unconditional — present even when
    # nothing is out of stock (#53).
    assert "never offer, quote, or agree a price on a product the catalog marks OUT OF STOCK" in captured["system"]
    assert "suggest an in-stock alternative" in captured["system"]


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


async def test_prompt_carries_gender_neutral_address_rule(db_session, shop_account):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="what do you sell?",
        from_phone="255711111112",
        db=db_session,
        account=shop_account,
    )

    # Transcript 255765268058: mirrored "kaka" locked in and survived
    # three corrections incl. "Mimi ni dada" (#99).
    assert "never assume the customer's gender" in captured["system"]
    assert "Do not mirror their address terms back" in captured["system"]
    assert "apologize WITHOUT repeating it" in captured["system"]


async def test_catalog_never_exposes_real_floor(db_session, shop_account, shoes):
    captured = {}
    service = _service_with_fake_client(captured=captured)

    await service.generate_response(
        message_body="bei ya mwisho?", from_phone="255711139001", db=db_session, account=shop_account,
    )

    assert "Lowest Price You May Offer TZS 44,000" in captured["system"]
    assert "40,000" not in captured["system"]
    assert "SECRET FLOOR" not in captured["system"]
