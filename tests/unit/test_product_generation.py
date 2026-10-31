"""AI product drafting (#90): parse the model reply strictly, persist
drafts only, never subscriptions or plans."""

import pytest

from app.db.models import Product, User
from app.services.product_generation import (
    GenerationError,
    generate_drafts,
    parse_generated_items,
)


@pytest.fixture
def merchant(db_session):
    user = User(username="255700000950")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class _FakeProvider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


class _FakeLLMService:
    def __init__(self, reply):
        self.provider = _FakeProvider(reply)


def _fake_llm(monkeypatch, reply):
    fake = _FakeLLMService(reply)
    monkeypatch.setattr("app.services.llm.llm_service", fake)
    return fake


# --- parse_generated_items -------------------------------------------------------


def test_parse_bare_array():
    items = parse_generated_items('[{"name": "Shoes", "max_price": 50000}]')

    assert items == [{"name": "Shoes", "max_price": 50000}]


def test_parse_products_object_and_fences():
    items = parse_generated_items('```json\n{"products": [{"name": "Shoes"}]}\n```')

    assert items == [{"name": "Shoes"}]


def test_parse_rejects_non_json():
    with pytest.raises(GenerationError):
        parse_generated_items("Here are your products: shoes, hats")


def test_parse_rejects_empty_list():
    with pytest.raises(GenerationError):
        parse_generated_items("[]")


def test_parse_caps_at_ten_items():
    items = parse_generated_items("[" + ",".join(f'{{"name": "P{i}"}}' for i in range(15)) + "]")

    assert len(items) == 10


# --- generate_drafts ---------------------------------------------------------------


async def test_drafts_created_from_summary(db_session, merchant, monkeypatch):
    fake = _fake_llm(
        monkeypatch,
        '[{"name": "iPhone 12", "description": "Used, 128GB", "max_price": 850000, '
        '"min_price": 800000, "category": "Phones", "sku": "IP12"}]',
    )

    drafts = await generate_drafts(db_session, merchant.id, "I sell used iPhones from 800k")

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.is_draft is True
    assert draft.name == "iPhone 12"
    assert draft.max_price == 850000
    assert draft.min_price == 800000
    assert draft.sku == "IP12"
    assert draft.is_subscription is False
    assert draft.is_platform_plan is False
    assert draft.slug.startswith("iphone-12")
    # The summary reached the model as the user message.
    assert fake.provider.calls[0]["messages"] == [{"role": "user", "content": "I sell used iPhones from 800k"}]


async def test_unusable_items_skipped_and_prices_sanitized(db_session, merchant, monkeypatch):
    _fake_llm(
        monkeypatch,
        '[{"name": "  "}, "not-a-dict", {"name": "Vase", "max_price": "lots", '
        '"min_price": 999999999, "description": 42}]',
    )

    drafts = await generate_drafts(db_session, merchant.id, "I sell vases and things")

    assert [d.name for d in drafts] == ["Vase"]
    assert drafts[0].max_price == 0
    # min above max collapses to max rather than persisting inverted.
    assert drafts[0].min_price == 0


async def test_colliding_sku_dropped(db_session, merchant, monkeypatch):
    db_session.add(Product(
        slug="existing", name="Existing", max_price=10, min_price=5,
        quantity=1, merchant_id=merchant.id, sku="DUP",
    ))
    db_session.commit()
    _fake_llm(monkeypatch, '[{"name": "New Thing", "sku": "DUP"}]')

    drafts = await generate_drafts(db_session, merchant.id, "one more product please")

    assert drafts[0].sku is None


async def test_garbage_reply_raises_without_writing(db_session, merchant, monkeypatch):
    _fake_llm(monkeypatch, "sorry, I cannot help with that")

    with pytest.raises(GenerationError):
        await generate_drafts(db_session, merchant.id, "a longer summary here")

    assert db_session.query(Product).count() == 0


async def test_no_llm_client_raises(db_session, merchant, monkeypatch):
    monkeypatch.setattr("app.services.llm.llm_service", None)

    with pytest.raises(RuntimeError):
        await generate_drafts(db_session, merchant.id, "a longer summary here")
