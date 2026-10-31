"""Merchant onboarding closes (#89).

Three breaks kept anyone from buying the Taskflow OS subscription (or any
subscription product): both close paths rejected quantity-0 products as
out of stock, the platform Amara couldn't see the product or close it,
and Instagram closes never provisioned the User row activation and
dashboard login need.
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
    Message,
    Order,
    Product,
    ProductSubscription,
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService, _is_out_of_stock

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
PLATFORM_SLUG = "taskflow-os-subscription"


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
        return _FakeResponse("Sure, here is some info.")

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000600")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def platform_owner(db_session):
    owner = User(username="255700000601")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def platform_account(db_session, platform_owner):
    account = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PLATFORM_ONBOARD",
        access_token="tok",
        user_id=platform_owner.id,
        is_platform_account=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def subscription_product(db_session, platform_owner):
    p = Product(
        slug=PLATFORM_SLUG,
        name="Taskflow OS",
        max_price=100000,
        min_price=100000,
        quantity=0,
        merchant_id=platform_owner.id,
        is_subscription=True,
        is_platform_plan=True,
        monthly_reply_limit=2000,
        description="Your 24/7 AI sales rep that closes.",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _mock_full_payment():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_ONBOARD"})
    )


def test_subscriptions_are_never_out_of_stock(db_session, merchant):
    sub = Product(
        slug="sub", name="Sub", max_price=100, min_price=100, quantity=0,
        merchant_id=merchant.id, is_subscription=True,
    )
    dead_sub = Product(
        slug="dead-sub", name="Dead", max_price=100, min_price=100, quantity=0,
        merchant_id=merchant.id, is_subscription=True, is_active=False,
    )
    goods = Product(
        slug="goods", name="Goods", max_price=100, min_price=100, quantity=0,
        merchant_id=merchant.id,
    )
    assert _is_out_of_stock(sub) is False
    assert _is_out_of_stock(dead_sub) is True
    assert _is_out_of_stock(goods) is True


@respx.mock
async def test_whatsapp_subscription_close_skips_stock(db_session, platform_account, subscription_product):
    _mock_full_payment()
    customer = Customer(phone_number="255711110601")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    service = _service_with_fake_client()
    note = await service._close_deal(
        db_session,
        platform_account,
        customer,
        "255711110601",
        {"product_name": "Taskflow OS", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "payment request" in note
    db_session.refresh(subscription_product)
    assert subscription_product.quantity == 0
    order = db_session.query(Order).filter(Order.customer_id == customer.id).one()
    assert order.status.value == "PENDING"
    subscription = (
        db_session.query(ProductSubscription)
        .filter(ProductSubscription.order_id == order.id)
        .one()
    )
    assert subscription.status.value == "PENDING_PAYMENT"


@respx.mock
async def test_instagram_subscription_close_provisions_user(
    db_session, merchant, platform_owner, subscription_product
):
    _mock_full_payment()
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_onboard",
        access_token="ig-tok",
        username="taskflow",
        ai_enabled=True,
        user_id=platform_owner.id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)
    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_onboard", username="prospect", account_id=ig_account.id
    )
    db_session.add(ig_customer)
    db_session.commit()
    db_session.refresh(ig_customer)

    service = _service_with_fake_client()
    await service._close_instagram_deal(
        db_session,
        ig_account,
        ig_customer,
        {"product_name": "Taskflow OS", "price": 100000, "payment_type": "full", "phone": "255711110602"},
    )

    user = db_session.query(User).filter(User.username == "255711110602").one()
    assert user is not None
    db_session.refresh(subscription_product)
    assert subscription_product.quantity == 0


@respx.mock
async def test_instagram_ordinary_close_provisions_no_user(db_session, merchant):
    _mock_full_payment()
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_shop",
        access_token="ig-tok",
        username="shop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)
    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_shopper", username="shopper", account_id=ig_account.id
    )
    db_session.add(ig_customer)
    db_session.commit()
    db_session.refresh(ig_customer)
    shoes = Product(
        slug="ig-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(shoes)
    db_session.commit()

    service = _service_with_fake_client()
    await service._close_instagram_deal(
        db_session,
        ig_account,
        ig_customer,
        {"product_name": "Shoes", "price": 45000, "payment_type": "full", "phone": "255711110603"},
    )

    assert db_session.query(User).filter(User.username == "255711110603").first() is None


async def test_platform_prompt_shows_subscription_and_close_mechanics(
    db_session, platform_account, subscription_product
):
    # A non-default price proves the prompt renders the product's price
    # rather than a hardcoded number.
    subscription_product.max_price = 150000
    subscription_product.min_price = 150000
    db_session.commit()
    db_session.add(
        Message(
            to_phone="255722220002",
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
        from_phone="255722220002",
        db=db_session,
        account=platform_account,
    )

    instruction = captured["system"]
    assert "Taskflow OS" in instruction
    assert "No products currently available" not in instruction
    assert "CLOSING MECHANICS" in instruction
    assert "AGREEMENT_REACHED" in instruction
    assert "Max Price TZS 150,000" in instruction
    assert "includes 2,000 Amara replies a month" in instruction
    assert "price=<that plan's catalog Max Price>" in instruction
    assert "100,000" not in instruction
    # The dashboard link renders from settings, never hardcoded (#91).
    assert "https://business.taskflowos.dev" in instruction
    assert "dashboard-taskflow" not in instruction


async def test_platform_prompt_without_plan_flag_has_no_catalog(
    db_session, platform_account, subscription_product
):
    # The product exists but isn't flagged as a plan — the pitch sees
    # nothing, the same as the old unset-slug behavior.
    subscription_product.is_platform_plan = False
    db_session.commit()
    db_session.add(
        Message(
            to_phone="255722220003",
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
        from_phone="255722220003",
        db=db_session,
        account=platform_account,
    )

    assert "No products currently available." in captured["system"]
