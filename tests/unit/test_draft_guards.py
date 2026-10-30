"""Draft guards (#74): a deal, quote nudge, or payment must never land on
an uncompleted import draft — even one a merchant stocked before
completing. The catalog builders exclude drafts from what the model sees;
these tests pin the server-side backstops for a marker that names one
anyway (prompt injection or a stale client)."""

import pytest

from app.db.models import (
    ConversationContext,
    Customer,
    InstagramAccount,
    InstagramCustomer,
    Order,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import DealRejected, LLMService


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000600")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Test Shop", phone_number_id="DRAFT_PHONE",
        access_token="tok", user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_draft_1", access_token="ig-tok",
        username="shopowner", ai_enabled=True, user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(
        ig_scoped_id="igsid_draft_1", username="buyer1", account_id=ig_account.id
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def stocked_draft(db_session, merchant):
    """A draft a merchant set stock on before completing — the dangerous
    shape: quantity alone would let a close proceed."""
    p = Product(
        slug="draft-stocked", name="Untitled import 99990000",
        max_price=0.0, min_price=0.0, quantity=5,
        is_active=False, is_draft=True, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _customer_and_context(db_session, phone):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return customer, context


async def test_whatsapp_close_rejects_stocked_draft(
    db_session, shop_account, stocked_draft, monkeypatch
):
    async def exploding_dispatch(*a, **kw):
        raise AssertionError("no payment dispatch for a draft close")

    monkeypatch.setattr(LLMService, "_dispatch_payment", exploding_dispatch)
    service = LLMService(api_key=None)
    customer, context = _customer_and_context(db_session, "255711140601")

    with pytest.raises(DealRejected, match="uncompleted draft"):
        await service._close_deal(
            db_session, shop_account, customer, "255711140601",
            {"product_name": stocked_draft.name, "price": 0, "payment_type": "full"},
            context,
        )

    assert db_session.query(Order).count() == 0


async def test_instagram_close_rejects_stocked_draft(
    db_session, ig_account, ig_customer, stocked_draft, monkeypatch
):
    async def exploding_dispatch(*a, **kw):
        raise AssertionError("no payment dispatch for a draft close")

    monkeypatch.setattr(LLMService, "_dispatch_payment", exploding_dispatch)
    service = LLMService(api_key=None)

    with pytest.raises(DealRejected, match="uncompleted draft"):
        await service._close_instagram_deal(
            db_session, ig_account, ig_customer,
            {"product_name": stocked_draft.name, "price": 0,
             "payment_type": "full", "phone": "255711140602"},
        )

    assert db_session.query(Order).count() == 0


async def test_track_quote_ignores_draft_product(
    db_session, shop_account, stocked_draft
):
    service = LLMService(api_key=None)
    _, context = _customer_and_context(db_session, "255711140603")

    await service._track_quote(
        db_session, shop_account, context,
        {"product_name": stocked_draft.name, "price": 0},
        "255711140603", "I'll take it", False,
    )

    # Nothing tracked: no intent, no pending quote, no nudge scheduled.
    assert context.current_product_intent is None
    assert context.pending_quote_price is None
    assert context.pending_quote_at is None
