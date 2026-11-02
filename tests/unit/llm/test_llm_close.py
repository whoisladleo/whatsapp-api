import httpx
import respx

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Installment,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
)
from app.services.llm import (
    LLMService,
    _holding_reply,
)


TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"
INSTALLMENT_URL = "https://pay.tunzaa.co.tz/installments/create"


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


# --- Deal closing: full end-to-end via generate_response --------------------


@respx.mock
async def test_agreement_full_payment_creates_order_and_dispatches(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_1"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take the shoes for 45000, full payment",
        from_phone="255711130000",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert "sent to your phone" in result
    assert "location" in result.lower()
    # WhatsApp orders should reference WhatsApp's own location-sharing
    # feature — see the Instagram-side regression test in
    # test_llm_dm_response.py for why this can't be generic/hardcoded.
    assert "WhatsApp's location-sharing feature" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order is not None
    assert order.product_id == shoes.id
    assert order.account_id == shop_account.id
    assert order.quantity == 1
    assert order.payment_type == PaymentType.FULL
    assert order.status == OrderStatus.PENDING
    assert order.reserved_until is not None
    assert order.tunzaa_transaction_id == "MOCK_TXN_1"
    assert payment_route.called

    db_session.refresh(shoes)
    assert shoes.quantity == 2  # reserved at creation time, decremented from 3

    customer = db_session.query(Customer).filter(Customer.phone_number == "255711130000").first()
    context = db_session.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
    assert context.last_offer == 45000


@respx.mock
async def test_agreement_for_subscription_product_creates_product_subscription(db_session, shop_account, merchant):
    subscription_product = Product(
        slug="taskflow-sub", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(subscription_product)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SUB_1"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Taskflow OS Subscription price=100000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="sign me up",
        from_phone="255711130050",
        db=db_session,
        account=shop_account,
    )

    order = db_session.query(Order).filter(Order.agreed_price == 100000).first()
    assert order is not None

    subscription = db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    assert subscription is not None
    assert subscription.product_id == subscription_product.id
    assert subscription.customer_id == order.customer_id
    assert subscription.instagram_customer_id is None
    assert subscription.monthly_amount == 100000
    assert subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT


@respx.mock
async def test_repeated_subscription_agreement_reuses_pending_subscription_row(db_session, shop_account, merchant):
    """
    Regression test: a customer who negotiates a subscription product twice
    before ever completing payment (e.g. abandons the first Tunzaa prompt
    and starts over) must not end up with a second PENDING order plus a
    second Tunzaa dispatch for the same deal — the re-close is blocked
    with an "already on its way" reminder instead (#101). The single
    PENDING_PAYMENT ProductSubscription row still belongs to that one
    order (row-level reuse now only matters on the pay_now path — see
    product_subscription.get_or_create_pending_subscription).
    """
    subscription_product = Product(
        slug="taskflow-sub-2", name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999999, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(subscription_product)
    db_session.commit()

    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_SUB_2"})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Taskflow OS Subscription price=100000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    first = await service.generate_response(
        message_body="sign me up",
        from_phone="255711130060",
        db=db_session,
        account=shop_account,
    )
    assert "payment request for TZS 100,000 has been sent" in first

    second = await service.generate_response(
        message_body="sign me up",
        from_phone="255711130060",
        db=db_session,
        account=shop_account,
    )
    assert "already on its way" in second

    orders = db_session.query(Order).filter(Order.agreed_price == 100000, Order.product_id == subscription_product.id).all()
    assert len(orders) == 1

    subscriptions = (
        db_session.query(ProductSubscription).filter(ProductSubscription.product_id == subscription_product.id).all()
    )
    assert len(subscriptions) == 1
    assert subscriptions[0].order_id == orders[0].id


@respx.mock
async def test_agreement_for_regular_product_does_not_create_product_subscription(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_2"})
    )

    async def handler(**kwargs):
        return _FakeResponse("Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]")

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="I'll take the shoes",
        from_phone="255711130051",
        db=db_session,
        account=shop_account,
    )

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order is not None
    assert db_session.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).count() == 0


@respx.mock
async def test_agreement_installment_creates_order_and_installment_rows(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "customer": {"customer_id": 1234, "first_name": "John"},
                "plan": {"plan_id": 1001, "status": "active", "total_amount": "45000.00"},
                "installments": [
                    {"installment_id": 10000, "installment_number": 1, "amount": "22500.00", "due_date": "2026-08-16", "status": "PENDING"},
                    {"installment_id": 10001, "installment_number": 2, "amount": "22500.00", "due_date": "2026-11-14", "status": "PENDING"},
                ],
            },
        )
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Sawa! [AGREEMENT_REACHED: product=Shoes price=45000 payment=installment]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll pay in installments",
        from_phone="255711130001",
        db=db_session,
        account=shop_account,
    )

    assert "installment plan has been created" in result
    assert "location" in result.lower()

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order.payment_type == PaymentType.INSTALLMENT
    assert order.tunzaa_plan_id == "1001"

    installments = db_session.query(Installment).filter(Installment.order_id == order.id).all()
    assert len(installments) == 2
    assert {i.tunzaa_installment_id for i in installments} == {"10000", "10001"}
    assert all(i.amount_due == 22500.0 for i in installments)


async def test_agreement_price_below_min_ignored(db_session, shop_account, shoes):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=1000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="1000 tzs please",
        from_phone="255711130002",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3  # untouched
    # The model's "Deal!" must not stand with no order behind it (#37).
    assert result == _holding_reply(None)
    assert db_session.query(ConversationContext).one().state == ConversationState.HUMAN_HANDOFF


async def test_agreement_unknown_product_ignored(db_session, shop_account, shoes):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Nonexistent Widget price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="widget please",
        from_phone="255711130003",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_out_of_stock_ignored(db_session, shop_account, shoes):
    shoes.quantity = 0
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130004",
        db=db_session,
        account=shop_account,
    )

    assert "AGREEMENT_REACHED" not in result
    assert db_session.query(Order).count() == 0


async def test_agreement_and_handoff_together_handoff_wins(db_session, shop_account, shoes, merchant, monkeypatch):
    async def fake_alert(self, to, name, language_code="en_US", components=None):
        return {"messages": [{"id": "wamid.alert"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", fake_alert)

    async def handler(**kwargs):
        return _FakeResponse(
            "Let me get a human. [HUMAN_HANDOFF] [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    await service.generate_response(
        message_body="actually let me talk to a person, and I'll take the shoes for 45000 full",
        from_phone="255711130005",
        db=db_session,
        account=shop_account,
    )

    assert db_session.query(Order).count() == 0
    db_session.refresh(shoes)
    assert shoes.quantity == 3


@respx.mock
async def test_agreement_tunzaa_dispatch_failure_releases_reservation(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130006",
        db=db_session,
        account=shop_account,
    )

    assert "went wrong setting up your payment" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).first()
    assert order.status == OrderStatus.CANCELLED

    db_session.refresh(shoes)
    assert shoes.quantity == 3  # decremented then released back


async def test_agreement_close_deal_exception_hands_off_with_holding_reply(db_session, shop_account, shoes, monkeypatch):
    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    async def raising_close_deal(*a, **kw):
        raise RuntimeError("unexpected DB error")

    monkeypatch.setattr(service, "_close_deal", raising_close_deal)

    result = await service.generate_response(
        message_body="I'll take it",
        from_phone="255711130007",
        db=db_session,
        account=shop_account,
    )

    # Not the generic Claude-error fallback, and not the model's "Deal!"
    # either — nothing follows it, so a human takes over (#37).
    assert result == _holding_reply(None)
    assert db_session.query(ConversationContext).one().state == ConversationState.HUMAN_HANDOFF
    assert db_session.query(Order).count() == 0


@respx.mock
async def test_agreement_installment_with_missing_due_date_defaults_gracefully(db_session, shop_account, shoes):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "plan": {"plan_id": 1001},
                "installments": [
                    {"installment_id": 1, "amount": "45000.00"},  # no due_date key at all
                ],
            },
        )
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Sawa! [AGREEMENT_REACHED: product=Shoes price=45000 payment=installment]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="installments please",
        from_phone="255711130009",
        db=db_session,
        account=shop_account,
    )

    assert "installment plan has been created" in result
    installment = db_session.query(Installment).filter(Installment.tunzaa_installment_id == "1").first()
    assert installment is not None
    assert installment.due_date is not None  # defaulted rather than raising


async def test_agreement_reminder_schedule_failure_does_not_break_order_creation(
    db_session, shop_account, shoes, monkeypatch
):
    from app.tasks.order_reminders import send_soft_nudge

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(send_soft_nudge, "apply_async", raising_apply_async)

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    import respx as respx_module
    with respx_module.mock:
        respx_module.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
        respx_module.post(PAYMENT_URL).mock(
            return_value=httpx.Response(200, json={"transactionID": "MOCK_TXN_2"})
        )
        result = await service.generate_response(
            message_body="I'll take it",
            from_phone="255711130010",
            db=db_session,
            account=shop_account,
        )

    assert "sent to your phone" in result
    assert db_session.query(Order).count() == 1


@respx.mock
async def test_agreement_on_platform_account_executes_close(db_session, platform_account, merchant):
    """#100 reverses the pre-#89 contract this test used to assert: the
    platform prompt emits AGREEMENT_REACHED for subscription closes, so
    the server executes it (a live transcript showed a prospect's yes
    producing nothing)."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "MOCK_TXN_PLATFORM"})
    )
    db_session.add(
        Product(slug="platform-plan", name="Plan", max_price=100, min_price=100, quantity=0, merchant_id=merchant.id, is_subscription=True)
    )
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse(
            "Sure! [AGREEMENT_REACHED: product=Plan price=100 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="Lets do this",
        from_phone="255711130008",
        db=db_session,
        account=platform_account,
    )

    assert "AGREEMENT_REACHED" not in result
    order = db_session.query(Order).one()
    assert order.status.value == "PENDING"
    assert order.tunzaa_transaction_id == "MOCK_TXN_PLATFORM"


async def test_agreement_still_suppressed_while_handed_off(db_session, platform_account, merchant):
    """The half of the old gate that survives #100: handoff wins over a
    close in the same conversation."""
    db_session.add(
        Product(slug="platform-plan", name="Plan", max_price=100, min_price=100, quantity=0, merchant_id=merchant.id, is_subscription=True)
    )
    customer = Customer(phone_number="255711130009")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF))
    db_session.commit()

    async def handler(**kwargs):
        return _FakeResponse(
            "Sure! [AGREEMENT_REACHED: product=Plan price=100 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="Lets do this",
        from_phone="255711130009",
        db=db_session,
        account=platform_account,
    )

    assert result is None
    assert db_session.query(Order).count() == 0


@respx.mock
async def test_dispatch_without_transaction_id_cancels_order_and_reports_failure(db_session, shop_account, shoes):
    """
    Regression: Tunzaa answered HTTP 200 but with no transactionID — no
    prompt actually went out, so the order must be CANCELLED (stock
    released) and the buyer told setup failed, never "sent".
    """
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-1"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True})
    )

    async def handler(**kwargs):
        return _FakeResponse(
            "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]"
        )

    service = _service_with_fake_client(handler=handler)

    result = await service.generate_response(
        message_body="I'll take the shoes for 45000, full payment",
        from_phone="255711130070",
        db=db_session,
        account=shop_account,
    )

    assert "has been sent" not in result
    assert "Something went wrong setting up your payment" in result

    order = db_session.query(Order).filter(Order.agreed_price == 45000).one()
    assert order.status == OrderStatus.CANCELLED
    assert order.tunzaa_transaction_id is None

    db_session.refresh(shoes)
    assert shoes.quantity == 3  # the claimed unit was released, not left reserved
