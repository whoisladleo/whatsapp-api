"""Close-flow guards from the Instagram outreach sims (#101).

The sims (sims/OUTREACH_SIM_REPORT.md, feat/ig-outreach-sims) caught Amara
re-emitting AGREEMENT_REACHED across turns — every emission minted a fresh
PENDING order plus a fresh Tunzaa dispatch — demanding a delivery address
for a digital subscription, and ending 5/32 conversations in silence on
empty model content. These tests pin the three fixes on both channels.
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
    OrderStatus,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService, _holding_reply

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


def _mock_full_payment(txn_id="MOCK_TXN_GUARD"):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    return respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": txn_id})
    )


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000900")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_guard",
        access_token="ig-tok",
        username="guardshop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_guard", username="guardbuyer", account_id=ig_account.id)
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
        name="Guard Shop", phone_number_id="GUARD_PHONE", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def sub_product(db_session, merchant):
    p = Product(
        slug="guard-sub", name="Guard Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _wa_customer_and_context(db_session, phone, language=None):
    customer = Customer(phone_number=phone, language=language)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return customer, context


@pytest.fixture
def guard_shoes(db_session, merchant):
    p = Product(
        slug="guard-shoes", name="Guard Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# --- Fix 1: no delivery ask for subscriptions -------------------------------


@respx.mock
async def test_ig_subscription_close_note_has_no_delivery_ask(
    db_session, ig_account, ig_customer, ig_context, sub_product
):
    _mock_full_payment()
    service, _ = _scripted_service(
        ["Deal! [AGREEMENT_REACHED: product=Guard Plan price=100000 payment=full phone=255711190001]"]
    )

    result = await service.generate_dm_response(
        "sign me up, 255711190001",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "payment request for TZS 100,000 has been sent" in result
    assert "delivery address" not in result
    assert "location" not in result.lower()


@respx.mock
async def test_wa_subscription_close_note_has_no_delivery_ask(
    db_session, shop_account, sub_product
):
    _mock_full_payment()
    service, _ = _scripted_service(
        ["Deal! [AGREEMENT_REACHED: product=Guard Plan price=100000 payment=full]"]
    )

    result = await service.generate_response(
        message_body="sign me up",
        from_phone="255711190002",
        db=db_session,
        account=shop_account,
    )

    assert "payment request for TZS 100,000 has been sent" in result
    assert "delivery" not in result.lower()
    assert "location" not in result.lower()


# --- Fixes 3+5: duplicate close blocked -------------------------------------


@respx.mock
async def test_ig_second_close_reuses_pending_order(
    db_session, ig_account, ig_customer, sub_product
):
    payment_route = _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    agreement = {"product_name": "Guard Plan", "price": 100000, "payment_type": "full", "phone": "255711190003"}

    first = await service._close_instagram_deal(db_session, ig_account, ig_customer, agreement)
    assert "payment request for TZS 100,000 has been sent" in first

    second = await service._close_instagram_deal(db_session, ig_account, ig_customer, agreement)
    assert "already on its way" in second

    customer = db_session.query(Customer).filter_by(phone_number="255711190003").one()
    orders = db_session.query(Order).filter_by(customer_id=customer.id).all()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.PENDING
    assert payment_route.call_count == 1


@respx.mock
async def test_wa_second_close_reuses_pending_order(db_session, shop_account, sub_product):
    payment_route = _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190004")
    agreement = {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"}

    first = await service._close_deal(
        db_session, shop_account, customer, "255711190004", agreement, context
    )
    assert "payment request for TZS 100,000 has been sent" in first

    second = await service._close_deal(
        db_session, shop_account, customer, "255711190004", agreement, context
    )
    assert "already on its way" in second

    orders = db_session.query(Order).filter_by(customer_id=customer.id).all()
    assert len(orders) == 1
    assert payment_route.call_count == 1


@respx.mock
async def test_ig_close_blocked_when_subscription_active(
    db_session, ig_account, ig_customer, sub_product
):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    agreement = {"product_name": "Guard Plan", "price": 100000, "payment_type": "full", "phone": "255711190005"}

    await service._close_instagram_deal(db_session, ig_account, ig_customer, agreement)
    customer = db_session.query(Customer).filter_by(phone_number="255711190005").one()
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    order.status = OrderStatus.PAID
    subscription = db_session.query(ProductSubscription).filter_by(order_id=order.id).one()
    subscription.status = ProductSubscriptionStatus.ACTIVE
    db_session.commit()

    # G1-branch t4: buyer paid, model closes again — no second order.
    note = await service._close_instagram_deal(db_session, ig_account, ig_customer, agreement)
    assert "already active" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1


@respx.mock
async def test_wa_close_blocked_when_subscription_active(db_session, shop_account, sub_product):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190006")
    agreement = {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"}

    await service._close_deal(db_session, shop_account, customer, "255711190006", agreement, context)
    order = db_session.query(Order).filter_by(customer_id=customer.id).one()
    order.status = OrderStatus.PAID
    subscription = db_session.query(ProductSubscription).filter_by(order_id=order.id).one()
    subscription.status = ProductSubscriptionStatus.ACTIVE
    db_session.commit()

    note = await service._close_deal(db_session, shop_account, customer, "255711190006", agreement, context)
    assert "already active" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1


# --- Fix 2: empty content retries once, then holding reply -------------------


async def test_ig_empty_retry_succeeds_second_attempt(
    db_session, ig_account, ig_customer, ig_context, sub_product
):
    service, calls = _scripted_service(["", "Karibu! How can I help?"])

    result = await service.generate_dm_response(
        "mambo",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert result == "Karibu! How can I help?"
    assert len(calls) == 2
    db_session.refresh(ig_context)
    assert ig_context.state != ConversationState.HUMAN_HANDOFF


async def test_ig_empty_twice_hands_off_with_holding(
    db_session, ig_account, ig_customer, ig_context, sub_product
):
    service, calls = _scripted_service([""])

    result = await service.generate_dm_response(
        "mambo",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert result == _holding_reply(None)
    assert len(calls) == 2
    db_session.refresh(ig_context)
    assert ig_context.state == ConversationState.HUMAN_HANDOFF


async def test_wa_empty_retry_succeeds_second_attempt(db_session, shop_account, sub_product):
    service, calls = _scripted_service(["", "Sijambo, nisaidie?"])

    result = await service.generate_response(
        message_body="mambo",
        from_phone="255711190007",
        db=db_session,
        account=shop_account,
    )

    assert result == "Sijambo, nisaidie?"
    assert len(calls) == 2


async def test_wa_empty_twice_hands_off_with_holding(db_session, shop_account, sub_product):
    service, calls = _scripted_service([""])

    result = await service.generate_response(
        message_body="mambo",
        from_phone="255711190008",
        db=db_session,
        account=shop_account,
    )

    assert result == _holding_reply(None)
    assert len(calls) == 2
    customer = db_session.query(Customer).filter_by(phone_number="255711190008").one()
    context = db_session.query(ConversationContext).filter_by(customer_id=customer.id).one()
    assert context.state == ConversationState.HUMAN_HANDOFF


# --- Same-language template notes -------------------------------------------


@respx.mock
async def test_wa_close_note_swahili_for_sw_customer(db_session, shop_account, sub_product):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190010", language="sw")

    note = await service._close_deal(
        db_session, shop_account, customer, "255711190010",
        {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "Ombi la malipo la TZS 100,000" in note
    assert "A payment request" not in note


@respx.mock
async def test_wa_close_note_english_for_en_customer(db_session, shop_account, sub_product):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190011", language="en")

    note = await service._close_deal(
        db_session, shop_account, customer, "255711190011",
        {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "A payment request for TZS 100,000 has been sent" in note
    assert "Ombi la malipo" not in note


@respx.mock
async def test_wa_close_note_bilingual_when_language_unknown(db_session, shop_account, sub_product):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190012")

    note = await service._close_deal(
        db_session, shop_account, customer, "255711190012",
        {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "Ombi la malipo la TZS 100,000" in note
    assert "A payment request for TZS 100,000 has been sent" in note


@respx.mock
async def test_wa_physical_close_delivery_ask_follows_language(db_session, shop_account, guard_shoes):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190013", language="sw")

    note = await service._close_deal(
        db_session, shop_account, customer, "255711190013",
        {"product_name": "Guard Shoes", "price": 45000, "payment_type": "full"},
        context,
    )

    assert "shiriki eneo lako" in note
    assert "delivery address" not in note
    assert "WhatsApp's location-sharing feature" not in note


@respx.mock
async def test_wa_reclose_guard_note_swahili_for_sw_customer(db_session, shop_account, sub_product):
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])
    customer, context = _wa_customer_and_context(db_session, "255711190014", language="sw")
    agreement = {"product_name": "Guard Plan", "price": 100000, "payment_type": "full"}

    await service._close_deal(db_session, shop_account, customer, "255711190014", agreement, context)
    second = await service._close_deal(db_session, shop_account, customer, "255711190014", agreement, context)

    assert "lishafika kwenye simu yako" in second
    assert "already on its way" not in second


@respx.mock
async def test_ig_close_note_bilingual_when_language_unknown(
    db_session, ig_account, ig_customer, sub_product
):
    # Instagram never classifies Customer.language, so IG notes stack both.
    _mock_full_payment()
    service, _ = _scripted_service(["deal"])

    note = await service._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Guard Plan", "price": 100000, "payment_type": "full", "phone": "255711190015"},
    )

    assert "Ombi la malipo la TZS 100,000" in note
    assert "A payment request for TZS 100,000 has been sent" in note


async def test_wa_shop_prompt_has_same_language_rule(db_session, shop_account, guard_shoes):
    service, calls = _scripted_service(["Sawa, bei ni TZS 45,000."])

    await service.generate_response(
        message_body="bei?",
        from_phone="255711190016",
        db=db_session,
        account=shop_account,
    )

    assert "SAME language the customer just used" in calls[0]["system"]
    assert "never mix Swahili and English within a single message" in calls[0]["system"]


# --- Amara closes the loop herself (#104) ------------------------------------


async def test_wa_shop_prompt_has_payment_truth_rule(db_session, shop_account, sub_product):
    service, calls = _scripted_service(["Sawa, bei ni sawa."])

    await service.generate_response(
        message_body="bei?",
        from_phone="255711190030",
        db=db_session,
        account=shop_account,
    )

    assert "Payment truth: you can't see Tunzaa's side" in calls[0]["system"]


async def test_ig_dm_prompt_has_payment_truth_rule(
    db_session, ig_account, ig_customer, ig_context, sub_product
):
    service, calls = _scripted_service(["Sawa."])

    await service.generate_dm_response(
        "bei?",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
    )

    assert "Payment truth: you can't see Tunzaa's side" in calls[0]["system"]


async def test_wa_platform_prompt_has_payment_truth_rule(db_session, merchant, sub_product):
    from app.db.models import Message, WhatsAppAccount

    platform_account = WhatsAppAccount(
        name="Taskflow", phone_number_id="LOOP_PLATFORM", access_token="tok",
        user_id=merchant.id, is_platform_account=True,
    )
    db_session.add(platform_account)
    db_session.commit()
    db_session.refresh(platform_account)
    # Not the first message (first messages get the verbatim welcome).
    db_session.add(Message(
        to_phone="255711190031", direction="outbound",
        account_id=platform_account.id, type="text", body="earlier",
    ))
    db_session.commit()
    service, calls = _scripted_service(["Sawa."])

    await service.generate_response(
        message_body="tell me more",
        from_phone="255711190031",
        db=db_session,
        account=platform_account,
    )

    assert "Payment truth: you can't see Tunzaa's side" in calls[0]["system"]


_loop_phone_counter = [0]


def _loop_order(db_session, merchant, language, is_subscription):
    from app.db.models import Order

    _loop_phone_counter[0] += 1
    customer = Customer(
        phone_number=f"25571119{_loop_phone_counter[0]:04d}", language=language
    )
    product = Product(
        slug=f"loop-{'sub' if is_subscription else 'goods'}-{language or 'none'}",
        name="Loop Plan" if is_subscription else "Loop Goods",
        max_price=100000, min_price=100000, quantity=0,
        merchant_id=merchant.id, is_subscription=is_subscription,
    )
    db_session.add_all([customer, product])
    db_session.commit()
    order = Order(customer_id=customer.id, product_id=product.id, status=OrderStatus.PAID)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_payment_notification_subscription_sw(db_session, merchant):
    from app.api.v1.tunzaa_webhook import _payment_notification_body

    body = _payment_notification_body(
        "payment_complete", _loop_order(db_session, merchant, "sw", True)
    )

    assert "Malipo yamethibitishwa" in body
    assert "Usajili wako umeanza" in body
    assert "kupelekewa" not in body
    assert "delivery" not in body.lower()


def test_payment_notification_subscription_en(db_session, merchant):
    from app.api.v1.tunzaa_webhook import _payment_notification_body

    body = _payment_notification_body(
        "payment_complete", _loop_order(db_session, merchant, "en", True)
    )

    assert "Payment confirmed! Thank you for your order." in body
    assert "subscription is now active" in body
    assert "delivery" not in body.lower()


def test_payment_notification_physical_keeps_delivery(db_session, merchant):
    from app.api.v1.tunzaa_webhook import _payment_notification_body

    body = _payment_notification_body(
        "payment_complete", _loop_order(db_session, merchant, "en", False)
    )

    assert "Payment confirmed! Thank you for your order." in body
    assert "We'll be in touch shortly about delivery." in body


def test_payment_notification_bilingual_when_language_unknown(db_session, merchant):
    from app.api.v1.tunzaa_webhook import _payment_notification_body

    body = _payment_notification_body(
        "payment_complete", _loop_order(db_session, merchant, None, True)
    )

    assert "Malipo yamethibitishwa" in body
    assert "Payment confirmed" in body


def test_payment_notification_failed_sw(db_session, merchant):
    from app.api.v1.tunzaa_webhook import _payment_notification_body

    body = _payment_notification_body(
        "payment_failed", _loop_order(db_session, merchant, "sw", False)
    )

    assert "hayakukamilika" in body
    assert "could not be completed" not in body
