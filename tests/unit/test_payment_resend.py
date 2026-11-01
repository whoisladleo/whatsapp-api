"""Payment-resend path and reclose-guard lazy-cancel (#113).

Prod showed Amara telling a buyer "it's on its way" with no order behind
it (a comma-formatted agreement price failed parsing silently), then
having no move at all when the buyer said the request never arrived.
These tests pin the three fixes: the [RESEND_PAYMENT] marker (re-fire /
dispatch-onto-live / close-fresh fallback), retiring expired PENDING
holds in the reclose guard instead of wedging on them, and the enriched
webhook rejection logs (last test lives in
tests/integration/test_tunzaa_webhook.py).
"""

from datetime import datetime, timedelta

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
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import (
    RESERVATION_WINDOW,
    LLMService,
    _hold_expired,
    _holding_reply,
    _parse_and_strip_resend,
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


def _scripted_service(responses):
    """Fake client that returns responses[0], responses[1], ... per call."""
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs)
        return _FakeResponse(responses[min(len(calls) - 1, len(responses) - 1)])

    service = LLMService(api_key=None)
    service.client = _FakeAnthropicClient(handler)
    return service, calls


def _mock_payments(txn_ids=("MOCK_TXN_RESEND",)):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    return respx.post(PAYMENT_URL).mock(
        side_effect=[
            httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": txn})
            for txn in txn_ids
        ]
    )


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000901")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    acct = WhatsAppAccount(
        name="Resend Shop", phone_number_id="RESEND_PHONE", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_resend",
        access_token="ig-tok",
        username="resendshop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_resend", username="resendbuyer", account_id=ig_account.id)
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
def resend_shoes(db_session, merchant):
    p = Product(
        slug="resend-shoes", name="Resend Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def resend_plan(db_session, merchant):
    p = Product(
        slug="resend-plan", name="Resend Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _wa_customer(db_session, phone):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return customer


# --- marker parsing --------------------------------------------------------


def test_parse_bare_marker():
    text, resend = _parse_and_strip_resend("Sorting it now [RESEND_PAYMENT]")

    assert resend == {}
    assert text == "Sorting it now"


def test_parse_params_marker_with_comma_price_and_phone():
    text, resend = _parse_and_strip_resend(
        "Sorting it [RESEND_PAYMENT: product=Resend Plan price=100,000 payment=full phone=255700000001]"
    )

    assert resend == {
        "product_name": "Resend Plan",
        "price": 100000.0,
        "payment_type": "full",
        "phone": "255700000001",
    }
    assert text == "Sorting it"


def test_parse_garbage_params_strips_and_degrades_to_bare():
    text, resend = _parse_and_strip_resend("Sorting it [RESEND_PAYMENT: frobnicate]")

    assert resend == {}
    assert text == "Sorting it"


def test_parse_no_marker_returns_none():
    text, resend = _parse_and_strip_resend("Just a normal reply")

    assert resend is None
    assert text == "Just a normal reply"


# --- hold expiry ------------------------------------------------------------


def test_hold_expired_future_reservation_is_live():
    order = Order(reserved_until=datetime.utcnow() + timedelta(hours=1))

    assert _hold_expired(order) is False


def test_hold_expired_past_reservation_is_expired():
    order = Order(reserved_until=datetime.utcnow() - timedelta(hours=1))

    assert _hold_expired(order) is True


def test_hold_expired_null_reservation_falls_back_to_age():
    old = Order(reserved_until=None, created_at=datetime.utcnow() - RESERVATION_WINDOW - timedelta(hours=1))
    new = Order(reserved_until=None, created_at=datetime.utcnow())

    assert _hold_expired(old) is True
    assert _hold_expired(new) is False


def test_hold_expired_no_timestamps_fails_safe():
    assert _hold_expired(Order(reserved_until=None, created_at=None)) is False


# --- reclose guard: expired holds retire instead of wedging -----------------


@respx.mock
async def test_wa_close_retires_expired_pending_and_closes_fresh(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_FIRST", "TXN_SECOND"))
    service, _ = _scripted_service(["deal"])
    customer = _wa_customer(db_session, "255711270001")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    agreement = {"product_name": "Resend Shoes", "price": 45000, "payment_type": "full"}

    first = await service._close_deal(
        db_session, shop_account, customer, "255711270001", agreement, context
    )
    assert "has been sent" in first
    assert db_session.get(Product, resend_shoes.id).quantity == 2

    stale = db_session.query(Order).filter_by(customer_id=customer.id).one()
    stale.reserved_until = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    second = await service._close_deal(
        db_session, shop_account, customer, "255711270001", agreement, context
    )
    assert "has been sent" in second
    assert "already on its way" not in second

    orders = db_session.query(Order).filter_by(customer_id=customer.id).order_by(Order.created_at).all()
    assert [o.status for o in orders] == [OrderStatus.CANCELLED, OrderStatus.PENDING]
    assert db_session.get(Product, resend_shoes.id).quantity == 2
    assert payment_route.call_count == 2


# --- WhatsApp resend ---------------------------------------------------------


@respx.mock
async def test_wa_resend_refires_against_live_order(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_CLOSE", "TXN_RESEND"))
    service, _ = _scripted_service(["deal"])
    customer = _wa_customer(db_session, "255711270002")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    await service._close_deal(
        db_session, shop_account, customer, "255711270002",
        {"product_name": "Resend Shoes", "price": 45000, "payment_type": "full"},
        context,
    )
    # The close just dispatched — move it outside the resend throttle window.
    db_session.query(Order).filter_by(customer_id=customer.id).one().updated_at = (
        datetime.utcnow() - timedelta(minutes=10)
    )
    db_session.commit()

    resend_service, _ = _scripted_service(
        ["Sorting it out now [RESEND_PAYMENT: product=Resend Shoes price=45000 payment=full]"]
    )
    result = await resend_service.generate_response(
        message_body="I never got the request",
        from_phone="255711270002",
        db=db_session,
        account=shop_account,
    )

    assert "re-sent the payment request" in result
    assert "RESEND_PAYMENT" not in result
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    assert order.status == OrderStatus.PENDING
    assert order.tunzaa_transaction_id == "TXN_RESEND"
    assert payment_route.call_count == 2


@respx.mock
async def test_wa_resend_throttles_back_to_back_prompts(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_CLOSE",))
    service, _ = _scripted_service(["deal"])
    customer = _wa_customer(db_session, "255711270003")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    await service._close_deal(
        db_session, shop_account, customer, "255711270003",
        {"product_name": "Resend Shoes", "price": 45000, "payment_type": "full"},
        context,
    )
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    order.updated_at = datetime.utcnow()
    db_session.commit()

    resend_service, _ = _scripted_service(["Sorting it [RESEND_PAYMENT]"])
    result = await resend_service.generate_response(
        message_body="still nothing",
        from_phone="255711270003",
        db=db_session,
        account=shop_account,
    )

    assert "just moments ago" in result
    assert db_session.query(Order).filter_by(customer_id=customer.id).one().tunzaa_transaction_id == "TXN_CLOSE"
    assert payment_route.call_count == 1


@respx.mock
async def test_wa_resend_dispatches_onto_undispatched_live_order(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_LATE",))
    customer = _wa_customer(db_session, "255711270004")
    db_session.add(
        Order(
            customer_id=customer.id,
            product_id=resend_shoes.id,
            account_id=shop_account.id,
            quantity=1,
            agreed_price=45000,
            payment_type=PaymentType.FULL,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + timedelta(hours=47),
        )
    )
    db_session.commit()

    service, _ = _scripted_service(["Sorting it [RESEND_PAYMENT]"])
    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270004",
        db=db_session,
        account=shop_account,
    )

    assert "has been sent" in result
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    assert order.tunzaa_transaction_id == "TXN_LATE"
    assert payment_route.call_count == 1


@respx.mock
async def test_wa_resend_with_no_live_order_closes_fresh(
    db_session, shop_account, resend_plan
):
    payment_route = _mock_payments(("TXN_FRESH",))
    service, _ = _scripted_service(
        ["Sorting it out now [RESEND_PAYMENT: product=Resend Plan price=100000 payment=full]"]
    )

    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270005",
        db=db_session,
        account=shop_account,
    )

    assert "has been sent" in result
    assert "RESEND_PAYMENT" not in result
    customer = db_session.query(Customer).filter_by(phone_number="255711270005").one()
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    assert order.status == OrderStatus.PENDING
    assert order.tunzaa_transaction_id == "TXN_FRESH"
    assert payment_route.call_count == 1


@respx.mock
async def test_wa_resend_refuses_installment_deals(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_NEVER",))
    service, _ = _scripted_service(
        ["Checking [RESEND_PAYMENT: product=Resend Shoes price=45000 payment=installment]"]
    )

    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270006",
        db=db_session,
        account=shop_account,
    )

    assert "Checking" in result
    assert "RESEND_PAYMENT" not in result
    customer = db_session.query(Customer).filter_by(phone_number="255711270006").one()
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 0
    assert payment_route.call_count == 0


@respx.mock
async def test_wa_resend_naming_another_product_closes_fresh(
    db_session, shop_account, resend_shoes, resend_plan
):
    payment_route = _mock_payments(("TXN_SHOES", "TXN_PLAN"))
    closer, _ = _scripted_service(["deal"])
    customer = _wa_customer(db_session, "255711270007")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    await closer._close_deal(
        db_session, shop_account, customer, "255711270007",
        {"product_name": "Resend Shoes", "price": 45000, "payment_type": "full"},
        context,
    )

    service, _ = _scripted_service(
        ["Sorting it [RESEND_PAYMENT: product=Resend Plan price=100000 payment=full]"]
    )
    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270007",
        db=db_session,
        account=shop_account,
    )

    assert "has been sent" in result
    orders = db_session.query(Order).filter_by(customer_id=customer.id).all()
    assert len(orders) == 2
    assert db_session.query(Order).filter_by(customer_id=customer.id, product_id=resend_plan.id).one().tunzaa_transaction_id == "TXN_PLAN"
    assert payment_route.call_count == 2


@respx.mock
async def test_wa_bare_resend_with_no_live_order_sends_words_alone(
    db_session, shop_account, resend_shoes
):
    payment_route = _mock_payments(("TXN_NEVER",))
    service, _ = _scripted_service(["Let me look into that for you [RESEND_PAYMENT]"])

    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270008",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me look into that for you"
    customer = db_session.query(Customer).filter_by(phone_number="255711270008").one()
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 0
    assert payment_route.call_count == 0


@respx.mock
async def test_wa_resend_fallback_failure_hands_off(
    db_session, shop_account, resend_shoes, monkeypatch
):
    async def _no_alert(*args, **kwargs):
        return None

    monkeypatch.setattr(LLMService, "_send_platform_alert_to_merchant", _no_alert)
    payment_route = _mock_payments(("TXN_NEVER",))
    service, _ = _scripted_service(
        ["Sorting it [RESEND_PAYMENT: product=No Such Thing price=100 payment=full]"]
    )

    result = await service.generate_response(
        message_body="I never got the request",
        from_phone="255711270009",
        db=db_session,
        account=shop_account,
    )

    customer = db_session.query(Customer).filter_by(phone_number="255711270009").one()
    assert result == _holding_reply(customer.language)
    context = db_session.query(ConversationContext).filter_by(customer_id=customer.id).one()
    assert context.state == ConversationState.HUMAN_HANDOFF
    assert payment_route.call_count == 0


# --- Instagram resend --------------------------------------------------------


@respx.mock
async def test_ig_resend_refires_against_live_order(
    db_session, ig_account, ig_customer, ig_context, resend_shoes
):
    payment_route = _mock_payments(("TXN_IG_CLOSE", "TXN_IG_RESEND"))
    closer, _ = _scripted_service(["deal"])
    await closer._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Resend Shoes", "price": 45000, "payment_type": "full", "phone": "255711270010"},
    )
    # The close just dispatched — move it outside the resend throttle window.
    db_session.query(Order).filter_by(instagram_customer_id=ig_customer.id).one().updated_at = (
        datetime.utcnow() - timedelta(minutes=10)
    )
    db_session.commit()

    service, _ = _scripted_service(["Sorting it [RESEND_PAYMENT]"])
    result = await service.generate_dm_response(
        "I never got the request",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "re-sent the payment request" in result
    assert "RESEND_PAYMENT" not in result
    order = db_session.query(Order).filter_by(instagram_customer_id=ig_customer.id).one()
    assert order.tunzaa_transaction_id == "TXN_IG_RESEND"
    assert payment_route.call_count == 2


@respx.mock
async def test_ig_resend_with_no_live_order_closes_fresh(
    db_session, ig_account, ig_customer, ig_context, resend_plan
):
    payment_route = _mock_payments(("TXN_IG_FRESH",))
    service, _ = _scripted_service(
        ["Sorting it [RESEND_PAYMENT: product=Resend Plan price=100000 payment=full phone=255711270011]"]
    )

    result = await service.generate_dm_response(
        "I never got the request",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "has been sent" in result
    order = db_session.query(Order).filter_by(instagram_customer_id=ig_customer.id).one()
    assert order.status == OrderStatus.PENDING
    assert order.tunzaa_transaction_id == "TXN_IG_FRESH"
    assert payment_route.call_count == 1


@respx.mock
async def test_ig_resend_fallback_without_phone_sends_words_alone(
    db_session, ig_account, ig_customer, ig_context, resend_plan
):
    payment_route = _mock_payments(("TXN_NEVER",))
    service, _ = _scripted_service(
        ["Sorting it [RESEND_PAYMENT: product=Resend Plan price=100000 payment=full]"]
    )

    result = await service.generate_dm_response(
        "I never got the request",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert result == "Sorting it"
    assert db_session.query(Order).filter_by(instagram_customer_id=ig_customer.id).count() == 0
    assert payment_route.call_count == 0


# --- prompts carry the new clauses -------------------------------------------


@respx.mock
async def test_wa_prompt_covers_resend_reopen_and_monthly_only(
    db_session, shop_account, resend_shoes
):
    _mock_payments(("TXN_UNUSED",))
    service, calls = _scripted_service(["hello there"])

    await service.generate_response(
        message_body="hello",
        from_phone="255711270012",
        db=db_session,
        account=shop_account,
    )

    system = calls[0]["system"]
    assert "RESEND_PAYMENT" in system
    assert "fresh start" in system
    assert "monthly only" in system
    assert "no address term in English" in system


@respx.mock
async def test_ig_prompt_covers_resend_with_phone_requirement(
    db_session, ig_account, ig_customer, ig_context, resend_shoes
):
    _mock_payments(("TXN_UNUSED",))
    service, calls = _scripted_service(["hello there"])

    await service.generate_dm_response(
        "hello",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    system = calls[0]["system"]
    assert "RESEND_PAYMENT" in system
    assert "phone number is required" in system
    assert "fresh start" in system
    assert "monthly only" in system
