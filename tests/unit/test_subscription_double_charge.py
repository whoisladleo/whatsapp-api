"""Subscription double-charge guards (#114).

Sandbox data showed one customer+product accumulating 4x PAID 100k orders
in two rapid pairs (16ms and 8s apart): the #101 reclose guard only blocks
a *pending* order or an *active* subscription, so a re-emitted close sails
through once the order flips PAID — and concurrent closes race straight
past the guard's read-then-write check. These tests pin both halves of the
fix: the partial unique index (one PENDING order per customer+product, the
DB decides the race) and the PAID-history block for subscriptions.
"""

from datetime import datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    ConversationContext,
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
from app.services import self_service_billing
from app.services.llm import LLMService

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"


def _mock_payments(txn_ids=("MOCK_TXN_DUP",)):
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
    owner = User(username="255700000902")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    acct = WhatsAppAccount(
        name="Dup Shop", phone_number_id="DUP_PHONE", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_account(db_session, merchant):
    acct = InstagramAccount(
        ig_business_id="ig_biz_dup",
        access_token="ig-tok",
        username="dupshop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_dup", username="dupbuyer", account_id=ig_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def dup_plan(db_session, merchant):
    p = Product(
        slug="dup-plan", name="Dup Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=merchant.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def platform_plan(db_session, merchant):
    p = Product(
        slug="dup-platform-plan", name="Dup Platform Plan", max_price=100000, min_price=100000,
        quantity=5, merchant_id=merchant.id, is_subscription=True, is_platform_plan=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def dup_shoes(db_session, merchant):
    p = Product(
        slug="dup-shoes", name="Dup Shoes", max_price=50000, min_price=40000,
        quantity=2, merchant_id=merchant.id,
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


def _pending_order(db_session, customer, product, account=None, ig_account=None, ig_customer=None,
                   txn_id="TXN_DUP_EXISTING"):
    order = Order(
        customer_id=customer.id,
        product_id=product.id,
        account_id=account.id if account else None,
        instagram_account_id=ig_account.id if ig_account else None,
        instagram_customer_id=ig_customer.id if ig_customer else None,
        quantity=1,
        agreed_price=product.max_price,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
        tunzaa_transaction_id=txn_id,
        reserved_until=datetime.utcnow() + timedelta(hours=47),
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def _paid_order(db_session, customer, product, account=None):
    order = Order(
        customer_id=customer.id,
        product_id=product.id,
        account_id=account.id if account else None,
        quantity=1,
        agreed_price=product.max_price,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PAID,
        tunzaa_transaction_id="TXN_DUP_PAID",
        reserved_until=datetime.utcnow() + timedelta(hours=47),
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


# --- the invariant itself ----------------------------------------------------


def test_second_pending_order_for_same_deal_violates_unique_index(
    db_session, shop_account, dup_plan
):
    customer = _wa_customer(db_session, "255711280001")
    _pending_order(db_session, customer, dup_plan, account=shop_account)

    db_session.add(
        Order(
            customer_id=customer.id,
            product_id=dup_plan.id,
            account_id=shop_account.id,
            quantity=1,
            agreed_price=100000,
            payment_type=PaymentType.FULL,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + timedelta(hours=48),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_full_order_coexists_with_inflight_installment_plan(
    db_session, shop_account, dup_plan
):
    # Pinned #110 behavior at the DB level: an in-flight installment plan
    # must not block (or be repurposed by) a FULL order for the same deal.
    customer = _wa_customer(db_session, "255711280009")
    db_session.add(
        Order(
            customer_id=customer.id,
            product_id=dup_plan.id,
            account_id=shop_account.id,
            quantity=1,
            agreed_price=100000,
            payment_type=PaymentType.INSTALLMENT,
            status=OrderStatus.PENDING,
            tunzaa_plan_id="plan-dup",
            reserved_until=datetime.utcnow() + timedelta(hours=48),
        )
    )
    db_session.commit()
    _pending_order(db_session, customer, dup_plan, account=shop_account)

    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 2


# --- PAID-history block for subscriptions -------------------------------------


@respx.mock
async def test_wa_close_blocked_by_paid_subscription_order(
    db_session, shop_account, dup_plan
):
    payment_route = _mock_payments(("TXN_DUP_NEVER",))
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280002")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    _paid_order(db_session, customer, dup_plan, account=shop_account)

    note = await service._close_deal(
        db_session, shop_account, customer, "255711280002",
        {"product_name": "Dup Plan", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "already active" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert payment_route.call_count == 0


@respx.mock
async def test_ig_close_blocked_by_paid_subscription_order(
    db_session, ig_account, ig_customer, dup_plan
):
    payment_route = _mock_payments(("TXN_DUP_NEVER",))
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280003")
    _paid_order(db_session, customer, dup_plan)

    note = await service._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Dup Plan", "price": 100000, "payment_type": "full", "phone": "255711280003"},
    )

    assert "already active" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert payment_route.call_count == 0


@respx.mock
async def test_consumable_rebuy_after_paid_still_flows(
    db_session, shop_account, dup_shoes
):
    payment_route = _mock_payments(("TXN_DUP_REBUY",))
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280004")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    _paid_order(db_session, customer, dup_shoes, account=shop_account)

    note = await service._close_deal(
        db_session, shop_account, customer, "255711280004",
        {"product_name": "Dup Shoes", "price": 45000, "payment_type": "full"},
        context,
    )

    assert "has been sent" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 2
    assert payment_route.call_count == 1


# --- race losers ---------------------------------------------------------------


@respx.mock
async def test_wa_close_race_loser_gets_already_sent_note(
    db_session, shop_account, dup_plan, monkeypatch
):
    payment_route = _mock_payments(("TXN_DUP_RACE",))
    monkeypatch.setattr("app.services.llm._reclose_guard_note", lambda *a, **k: None)
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280005")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    _pending_order(db_session, customer, dup_plan, account=shop_account)

    note = await service._close_deal(
        db_session, shop_account, customer, "255711280005",
        {"product_name": "Dup Plan", "price": 100000, "payment_type": "full"},
        context,
    )

    assert "already on its way" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert payment_route.call_count == 0
    # The loser's session must still be usable after the rollback.
    assert db_session.query(Customer).filter_by(phone_number="255711280005").one() is not None


@respx.mock
async def test_wa_close_race_loser_restores_claimed_stock(
    db_session, shop_account, dup_shoes, monkeypatch
):
    payment_route = _mock_payments(("TXN_DUP_RACE",))
    monkeypatch.setattr("app.services.llm._reclose_guard_note", lambda *a, **k: None)
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280006")
    context = ConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    _pending_order(db_session, customer, dup_shoes, account=shop_account)

    note = await service._close_deal(
        db_session, shop_account, customer, "255711280006",
        {"product_name": "Dup Shoes", "price": 45000, "payment_type": "full"},
        context,
    )

    assert "already on its way" in note
    assert db_session.get(Product, dup_shoes.id).quantity == 2
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert payment_route.call_count == 0


@respx.mock
async def test_ig_close_race_loser_gets_already_sent_note(
    db_session, ig_account, ig_customer, dup_plan, monkeypatch
):
    payment_route = _mock_payments(("TXN_DUP_RACE",))
    monkeypatch.setattr("app.services.llm._reclose_guard_note", lambda *a, **k: None)
    service = LLMService(api_key=None)
    customer = _wa_customer(db_session, "255711280007")
    _pending_order(db_session, customer, dup_plan, ig_account=ig_account, ig_customer=ig_customer)

    note = await service._close_instagram_deal(
        db_session, ig_account, ig_customer,
        {"product_name": "Dup Plan", "price": 100000, "payment_type": "full", "phone": "255711280007"},
    )

    assert "already on its way" in note
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert payment_route.call_count == 0


@respx.mock
async def test_pay_now_race_reuses_winner(
    db_session, merchant, platform_plan, monkeypatch
):
    payment_route = _mock_payments(("TXN_DUP_PAYNOW",))
    user = User(username="255711280008")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    customer = _wa_customer(db_session, "255711280008")
    winner = _pending_order(db_session, customer, platform_plan, txn_id=None)

    real_find = self_service_billing._find_reusable_order
    calls = {"n": 0}

    def _miss_once(db, customer, product):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(db, customer, product)

    monkeypatch.setattr(self_service_billing, "_find_reusable_order", _miss_once)

    result = await self_service_billing.pay_now(db_session, user, "255711280008")

    assert result["order_id"] == winner.id
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1
    assert db_session.get(Product, platform_plan.id).quantity == 5
    assert payment_route.call_count == 1
