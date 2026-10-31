"""Atomic stock decrement at deal close (#54).

_close_deal (WhatsApp) and _close_instagram_deal (IG) both used to check
quantity and then do a read-modify-write `product.quantity -= 1`, so two
concurrent closes on the last unit could both succeed. Both paths now
enforce the decrement with a conditional UPDATE
(SET quantity = quantity - 1 WHERE id = ... AND quantity >= 1, same claim
style as claim_available_pool_number); a 0-row update raises DealRejected
exactly like the existing out-of-stock pre-check.

Race-test approach: true thread-concurrency is infeasible on this suite's
SQLite setup (a single in-memory engine on StaticPool — one shared
connection that serializes everything — and Sessions are not
thread-safe), so the race is simulated deterministically instead: the
claim is stubbed/sequenced so the second close sees 0 rows, and we assert
DealRejected plus a single order.
"""

import httpx
import pytest
import respx

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import (
    DealRejected,
    LLMService,
    _holding_reply,
    _try_claim_stock,
)

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"


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


def _service_with_fake_client(handler=None):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        return _FakeResponse("Sure, here is some info.")

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    """These tests stub an Anthropic-shaped client, so pin the provider they
    exercise to Claude regardless of ambient LLM_PROVIDER."""
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000500")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="CLAIM_PHONE",
        access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_claim_1",
        access_token="ig-tok",
        username="shopowner",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_claim_1", username="buyer1", account_id=ig_account.id)
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
def shoes(db_session, merchant):
    p = Product(
        slug="claim-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _mock_full_payment():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_CLAIM"})
    )


# --- Claim primitive ------------------------------------------------------


def test_try_claim_stock_first_wins_second_loses(db_session, merchant):
    """Two claims against a single unit: exactly one wins, quantity never
    goes negative — the deterministic core of the double-close race."""
    p = Product(
        slug="last-unit", name="Last Unit", max_price=50000, min_price=40000,
        quantity=1, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    assert _try_claim_stock(db_session, p) is True
    assert p.quantity == 0

    assert _try_claim_stock(db_session, p) is False
    db_session.refresh(p)
    assert p.quantity == 0


def test_try_claim_stock_zero_quantity_loses(db_session, merchant):
    p = Product(
        slug="empty", name="Empty", max_price=50000, min_price=40000,
        quantity=0, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    assert _try_claim_stock(db_session, p) is False
    db_session.refresh(p)
    assert p.quantity == 0


# --- 0-row claim maps to DealRejected (both paths) --------------------------


async def test_close_deal_lost_race_raises_deal_rejected(db_session, shop_account, shoes, monkeypatch):
    """Pre-check passes (stock on hand) but the claim sees 0 rows — a
    concurrent close took the last unit first. Must raise DealRejected
    with no order and untouched stock."""
    monkeypatch.setattr("app.services.llm._try_claim_stock", lambda db, product: False)

    customer = Customer(phone_number="255711130501")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    service = _service_with_fake_client()

    with pytest.raises(DealRejected):
        await service._close_deal(
            db_session, shop_account, customer, "255711130501",
            {"product_name": "Shoes", "price": 45000, "payment_type": "full"},
            context,
        )

    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3


async def test_close_instagram_deal_lost_race_raises_deal_rejected(
    db_session, ig_account, ig_customer, shoes, monkeypatch
):
    monkeypatch.setattr("app.services.llm._try_claim_stock", lambda db, product: False)

    service = _service_with_fake_client()

    with pytest.raises(DealRejected):
        await service._close_instagram_deal(
            db_session, ig_account, ig_customer,
            {"product_name": "Shoes", "price": 45000, "payment_type": "full", "phone": "255711130502"},
        )

    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3


# --- 0-row path funnels into the identical holding-reply + handoff ----------


async def test_lost_race_funnels_to_same_holding_reply_whatsapp(db_session, shop_account, shoes, monkeypatch):
    """The 0-row DealRejected must produce the exact same customer-visible
    reply and handoff as every other DealRejected — no new reply text."""
    monkeypatch.setattr("app.services.llm._try_claim_stock", lambda db, product: False)

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130503",
        db=db_session,
        account=shop_account,
    )

    assert result == _holding_reply(None)
    assert db_session.query(Order).count() == 0
    assert db_session.query(ConversationContext).one().state == ConversationState.HUMAN_HANDOFF


async def test_lost_race_funnels_to_same_holding_reply_instagram(
    db_session, ig_account, ig_customer, ig_context, shoes, monkeypatch
):
    monkeypatch.setattr("app.services.llm._try_claim_stock", lambda db, product: False)

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130504]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_dm_response(
        "I'll take it", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )

    assert result == _holding_reply(None)
    assert db_session.query(Order).count() == 0
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


# --- Normal close still decrements by exactly 1 (both paths) ---------------


@respx.mock
async def test_close_deal_decrements_exactly_one(db_session, shop_account, shoes):
    _mock_full_payment()

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130505",
        db=db_session,
        account=shop_account,
    )

    assert db_session.query(Order).count() == 1
    db_session.refresh(shoes)
    assert shoes.quantity == 2


@respx.mock
async def test_close_instagram_deal_decrements_exactly_one(
    db_session, ig_account, ig_customer, ig_context, shoes
):
    _mock_full_payment()

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130506]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_dm_response(
        "I'll take it", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )

    assert db_session.query(Order).count() == 1
    db_session.refresh(shoes)
    assert shoes.quantity == 2


# --- Double close on the last unit: exactly one order -----------------------


async def test_double_close_last_unit_exactly_one_order(db_session, shop_account, shoes, monkeypatch):
    """Simulates the concurrent double-close deterministically: the first
    close claims the last unit and creates the order; the second close read
    the row before the first commit (stale quantity 1 in its pre-check
    view, held in-session without flushing), so it passes the pre-check and
    its conditional UPDATE lands after and sees 0 rows (stubbed claim) —
    DealRejected instead of a second order. Asserts the stub fired so this
    stays a genuine 0-row race test rather than a pre-check test."""
    shoes.quantity = 1
    db_session.commit()

    async def fake_dispatch(self, db, order, product, phone, payment_type, language=None):
        return "payment note"

    monkeypatch.setattr(LLMService, "_dispatch_payment", fake_dispatch)

    service = _service_with_fake_client()

    async def make_customer_context(phone):
        customer = Customer(phone_number=phone)
        db_session.add(customer)
        db_session.commit()
        db_session.refresh(customer)
        context = ConversationContext(customer_id=customer.id)
        db_session.add(context)
        db_session.commit()
        db_session.refresh(context)
        return customer, context

    customer_a, context_a = await make_customer_context("255711130507")
    await service._close_deal(
        db_session, shop_account, customer_a, "255711130507",
        {"product_name": "Shoes", "price": 45000, "payment_type": "full"},
        context_a,
    )

    # Second close: its conditional UPDATE lands after the first commit
    # and matches 0 rows (stubbed claim). Its pre-check view is held stale
    # at quantity 1 — as read before the first commit — so it reaches the
    # stub instead of stopping at the pre-check. The session runs with
    # autoflush=False, so this in-memory write never reaches the DB: the
    # second close's query returns this same identity-mapped instance and
    # passes the pre-check. Must be set after make_customer_context (whose
    # commits would flush it) and rolled back after the rejection.
    claim_calls = []

    def stub_claim(db, product):
        claim_calls.append(1)
        return False

    monkeypatch.setattr("app.services.llm._try_claim_stock", stub_claim)
    customer_b, context_b = await make_customer_context("255711130508")
    shoes.quantity = 1  # stale pre-check view; unflushed, discarded below
    with pytest.raises(DealRejected):
        await service._close_deal(
            db_session, shop_account, customer_b, "255711130508",
            {"product_name": "Shoes", "price": 45000, "payment_type": "full"},
            context_b,
        )
    assert claim_calls == [1]
    db_session.rollback()  # discard the stale in-memory quantity

    assert db_session.query(Order).count() == 1
    db_session.refresh(shoes)
    assert shoes.quantity == 0


# --- NULL quantity (merchant PATCH quantity=null) rejects as out-of-stock ---


async def test_close_deal_null_quantity_raises_deal_rejected(db_session, shop_account, merchant):
    """A NULL quantity row (reachable via merchant PATCH quantity=null
    persisting None) must reject as out-of-stock DealRejected, not raise
    TypeError from the pre-check comparison."""
    p = Product(
        slug="null-qty", name="NullQty", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    # An UPDATE, like the merchant PATCH path — a Python-side column
    # default would replace an explicit None on INSERT, so NULL is only
    # reachable this way.
    p.quantity = None
    db_session.commit()
    db_session.refresh(p)
    assert p.quantity is None

    customer = Customer(phone_number="255711130509")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    service = _service_with_fake_client()

    with pytest.raises(DealRejected):
        await service._close_deal(
            db_session, shop_account, customer, "255711130509",
            {"product_name": "NullQty", "price": 45000, "payment_type": "full"},
            context,
        )

    assert db_session.query(Order).count() == 0
