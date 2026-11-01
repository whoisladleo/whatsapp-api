from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.db.models import (
    ConversationContext,
    Customer,
    InstagramAccount,
    InstagramCustomer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services import support_access
from app.services.llm import DealRejected, LLMService
from app.services.merchant_handler import handle_merchant_message
from app.services.payment_resend import (
    ResendOutcome,
    _fresh_clone,
    merchant_resend_order,
    resend_live_order,
)

TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"


@pytest.fixture
def owner(db_session):
    user = User(username="255700000920")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Resend Shop", phone_number_id="RESEND_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255722220001")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="resend-widget", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=owner.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def sub_product(db_session, owner):
    p = Product(
        slug="resend-plan", name="Resend Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def ig_account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_resend", access_token="ig-tok",
        username="resendshop", ai_enabled=True, user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_resend", username="resender", account_id=ig_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _mock_tunzaa(txn_id="TXN-RESEND-1"):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": txn_id})
    )


def _order(db_session, customer, product, account=None, ig=None, status=OrderStatus.PENDING,
           payment_type=PaymentType.FULL, txn="TXN-OLD", updated_hours_ago=1, hold_hours=2, price=None):
    order = Order(
        customer_id=customer.id if customer else None,
        product_id=product.id,
        account_id=account.id if account else None,
        instagram_account_id=ig[0].id if ig else None,
        instagram_customer_id=ig[1].id if ig else None,
        quantity=1,
        agreed_price=price if price is not None else product.max_price,
        payment_type=payment_type,
        status=status,
        tunzaa_transaction_id=txn,
        updated_at=datetime.utcnow() - timedelta(hours=updated_hours_ago),
        reserved_until=datetime.utcnow() + timedelta(hours=hold_hours),
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def _closer(monkeypatch):
    monkeypatch.setattr("app.services.llm.llm_service", LLMService(api_key=None))


# --- resend_live_order ---


@respx.mock
async def test_live_resend_updates_transaction(db_session, customer, product, account):
    _mock_tunzaa()
    order = _order(db_session, customer, product, account=account)

    result = await resend_live_order(db_session, order, customer.phone_number)

    assert result.outcome is ResendOutcome.RESENT
    assert result.order.id == order.id
    db_session.refresh(order)
    assert order.tunzaa_transaction_id == "TXN-RESEND-1"


@respx.mock
async def test_live_resend_throttles_within_window(db_session, customer, product, account):
    order = _order(db_session, customer, product, account=account, updated_hours_ago=0)
    order.updated_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    result = await resend_live_order(db_session, order, customer.phone_number)

    assert result.outcome is ResendOutcome.THROTTLED
    assert 0 < result.retry_after_seconds <= 5 * 60


@respx.mock
async def test_live_resend_dispatches_first_attempt(db_session, customer, product, account):
    _mock_tunzaa()
    order = _order(db_session, customer, product, account=account, txn=None)

    result = await resend_live_order(db_session, order, customer.phone_number)

    assert result.outcome is ResendOutcome.RESENT
    db_session.refresh(order)
    assert order.tunzaa_transaction_id == "TXN-RESEND-1"


async def test_live_resend_refuses_installment(db_session, customer, product, account):
    order = _order(db_session, customer, product, account=account, payment_type=PaymentType.INSTALLMENT)

    result = await resend_live_order(db_session, order, customer.phone_number)

    assert result.outcome is ResendOutcome.INSTALLMENT_REFUSED


@respx.mock
async def test_live_resend_reports_tunzaa_outage(db_session, customer, product, account):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500, json={}))
    order = _order(db_session, customer, product, account=account)

    result = await resend_live_order(db_session, order, customer.phone_number)

    assert result.outcome is ResendOutcome.TUNZAA_ERROR
    db_session.refresh(order)
    assert order.tunzaa_transaction_id == "TXN-OLD"
    assert order.status == OrderStatus.PENDING


# --- merchant_resend_order: routing ---


async def test_merchant_resend_rejects_settled_order(db_session, customer, product, account):
    order = _order(db_session, customer, product, account=account, status=OrderStatus.PAID)

    result = await merchant_resend_order(db_session, order)

    assert result.outcome is ResendOutcome.NOT_PENDING


async def test_merchant_resend_refuses_installment_with_schedule_detail(
    db_session, customer, product, account
):
    order = _order(db_session, customer, product, account=account, payment_type=PaymentType.INSTALLMENT)

    result = await merchant_resend_order(db_session, order)

    assert result.outcome is ResendOutcome.INSTALLMENT_REFUSED
    assert "Tunzaa's own schedule" in result.detail


@respx.mock
async def test_merchant_resend_live_order_refires(db_session, customer, product, account):
    _mock_tunzaa()
    order = _order(db_session, customer, product, account=account)

    result = await merchant_resend_order(db_session, order)

    assert result.outcome is ResendOutcome.RESENT
    assert result.order.id == order.id


# --- merchant_resend_order: fresh clone ---


@respx.mock
async def test_expired_hold_clones_fresh_order_same_terms(
    db_session, monkeypatch, customer, product, account
):
    _closer(monkeypatch)
    _mock_tunzaa(txn_id="TXN-FRESH-1")
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    db_session.refresh(product)
    stock_before = product.quantity

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.FRESH_ORDERED
    fresh = result.order
    assert fresh.id != old.id
    assert fresh.status == OrderStatus.PENDING
    assert fresh.agreed_price == old.agreed_price == 50000
    assert fresh.product_id == product.id
    assert fresh.tunzaa_transaction_id == "TXN-FRESH-1"
    assert fresh.reserved_until > datetime.utcnow()
    db_session.refresh(old)
    assert old.status == OrderStatus.CANCELLED
    # Old claim released, new claim taken: net zero.
    db_session.refresh(product)
    assert product.quantity == stock_before
    # One PENDING per deal — the cancelled row can't collide with the fresh one.
    assert (
        db_session.query(Order)
        .filter_by(customer_id=customer.id, product_id=product.id, status=OrderStatus.PENDING)
        .count()
        == 1
    )


@respx.mock
async def test_expired_clone_fails_clean_on_stockout(db_session, monkeypatch, customer, product, account):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    product.quantity = 0
    db_session.commit()

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.OUT_OF_STOCK
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING  # untouched, pre-flight failed first
    assert (
        db_session.query(Order).filter_by(customer_id=customer.id, product_id=product.id).count() == 1
    )


@respx.mock
async def test_expired_clone_fails_clean_on_drafted_product(db_session, monkeypatch, customer, product, account):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    product.is_draft = True
    db_session.commit()

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.DEAL_INVALID
    assert "no longer available" in result.detail
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING


@respx.mock
async def test_expired_clone_blocked_when_plan_already_paid(
    db_session, monkeypatch, customer, sub_product, account
):
    _closer(monkeypatch)
    _mock_tunzaa()
    _order(db_session, customer, sub_product, account=account, status=OrderStatus.PAID, txn="TXN-PAID")
    old = _order(db_session, customer, sub_product, account=account, hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.ALREADY_OWNED
    # Old row restored exactly — history stays truthful.
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING
    assert old.reserved_until < datetime.utcnow()
    assert (
        db_session.query(Order)
        .filter_by(customer_id=customer.id, product_id=sub_product.id, status=OrderStatus.PENDING)
        .count()
        == 1
    )


@respx.mock
async def test_expired_instagram_order_clones_via_ig_close(
    db_session, monkeypatch, customer, product, ig_account, ig_customer
):
    _closer(monkeypatch)
    _mock_tunzaa(txn_id="TXN-IG-FRESH")
    old = _order(db_session, customer, product, ig=(ig_account, ig_customer), hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.FRESH_ORDERED
    assert result.order.instagram_customer_id == ig_customer.id
    assert result.order.tunzaa_transaction_id == "TXN-IG-FRESH"
    db_session.refresh(old)
    assert old.status == OrderStatus.CANCELLED


@respx.mock
async def test_instagram_order_without_phone_has_nowhere_to_prompt(
    db_session, monkeypatch, product, ig_account, ig_customer
):
    _closer(monkeypatch)
    old = _order(db_session, None, product, ig=(ig_account, ig_customer), hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.NO_PHONE


async def test_clone_refuses_when_channel_rows_are_gone(db_session, monkeypatch, customer, product, account):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    db_session.delete(account)
    db_session.commit()

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.DEAL_INVALID
    assert "channel is gone" in result.detail


async def test_clone_refuses_when_instagram_account_is_gone(
    db_session, monkeypatch, customer, product, ig_account, ig_customer
):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, ig=(ig_account, ig_customer), hold_hours=-3)
    db_session.delete(ig_account)
    db_session.commit()

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.DEAL_INVALID


async def test_clone_unavailable_without_closer(db_session, monkeypatch, customer, product, account):
    monkeypatch.setattr("app.services.llm.llm_service", None)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.TUNZAA_ERROR


async def test_clone_refuses_repriced_deal(db_session, monkeypatch, customer, product, account):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    product.max_price = 10000
    product.min_price = 5000
    db_session.commit()

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.DEAL_INVALID
    assert "no longer matches" in result.detail
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING


def _crashing_closer(monkeypatch, error):
    service = LLMService(api_key=None)

    async def _boom(*args, **kwargs):
        raise error

    service._close_deal = _boom
    service._close_instagram_deal = _boom
    monkeypatch.setattr("app.services.llm.llm_service", service)


async def test_clone_restores_row_on_close_crash(db_session, monkeypatch, customer, product, account):
    _crashing_closer(monkeypatch, RuntimeError("tunzaa down"))
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    db_session.refresh(product)
    stock_before = product.quantity

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.TUNZAA_ERROR
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING
    assert old.reserved_until < datetime.utcnow()
    db_session.refresh(product)
    assert product.quantity == stock_before


async def test_clone_restores_row_on_late_stockout(db_session, monkeypatch, customer, product, account):
    _crashing_closer(monkeypatch, DealRejected("AGREEMENT_REACHED for 'Widget' but it's out of stock"))
    old = _order(db_session, customer, product, account=account, hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.OUT_OF_STOCK
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING


async def test_clone_restores_row_on_late_rejection(db_session, monkeypatch, customer, product, account):
    _crashing_closer(monkeypatch, DealRejected("AGREEMENT_REACHED for 'Widget' but it's an uncompleted draft"))
    old = _order(db_session, customer, product, account=account, hold_hours=-3)

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.DEAL_INVALID
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING


async def test_clone_without_landing_order_restores_and_reports_owned(
    db_session, monkeypatch, customer, product, account
):
    # Defensive path: the close returned a note but no new PENDING row
    # exists (the PAID-subscription guard shape) — the old row comes
    # back exactly, stock adjustment reversed.
    service = LLMService(api_key=None)

    async def _note_only(*args, **kwargs):
        return "already on its way"

    service._close_deal = _note_only
    monkeypatch.setattr("app.services.llm.llm_service", service)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)
    db_session.refresh(product)
    stock_before = product.quantity

    result = await merchant_resend_order(db_session, old)

    assert result.outcome is ResendOutcome.ALREADY_OWNED
    db_session.refresh(old)
    assert old.status == OrderStatus.PENDING
    db_session.refresh(product)
    assert product.quantity == stock_before


async def test_fresh_clone_directly_refuses_phoneless_parties(
    db_session, monkeypatch, customer, product, account
):
    _closer(monkeypatch)
    old = _order(db_session, customer, product, account=account, hold_hours=-3)

    result = await _fresh_clone(db_session, old, (account, customer, None, None, False))

    assert result.outcome is ResendOutcome.NO_PHONE


# --- /resend command ---


async def test_resend_command_usage(db_session, account):
    assert await handle_merchant_message(db_session, account, "/resend", "255700000920") == (
        "Usage: `/resend 2557XXXXXXXX` (WhatsApp customer) or "
        "`/resend @their_instagram_username` (Instagram customer)"
    )


async def test_resend_command_no_pending_orders(db_session, account, customer):
    assert (
        await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")
        == "No pending orders for 255722220001."
    )


@respx.mock
async def test_resend_command_refires_single_live_order(db_session, account, customer, product):
    _mock_tunzaa()
    _order(db_session, customer, product, account=account)

    reply = await handle_merchant_message(db_session, account, "/resend 0722220001", "255700000920")

    assert reply == "Payment prompt re-sent to 255722220001 for Widget (TZS 50,000)."


async def test_resend_command_refuses_ambiguous_without_guessing(
    db_session, owner, account, customer, product
):
    other = Product(
        slug="resend-gadget", name="Gadget", max_price=30000, min_price=25000,
        quantity=5, merchant_id=owner.id,
    )
    db_session.add(other)
    db_session.commit()
    _order(db_session, customer, product, account=account)
    _order(db_session, customer, other, account=account)

    reply = await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")

    assert "more than one pending order" in reply
    assert "Widget — TZS 50,000" in reply
    assert "Gadget — TZS 30,000" in reply
    assert "dashboard" in reply


async def test_resend_command_explains_installment_only(db_session, account, customer, product):
    _order(db_session, customer, product, account=account, payment_type=PaymentType.INSTALLMENT)

    reply = await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")

    assert "installment plan" in reply
    assert "Tunzaa's own schedule" in reply


@respx.mock
async def test_resend_command_clones_expired_hold(db_session, monkeypatch, account, customer, product):
    _closer(monkeypatch)
    _mock_tunzaa(txn_id="TXN-CMD-FRESH")
    _order(db_session, customer, product, account=account, hold_hours=-3)

    reply = await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")

    assert reply == (
        "The old hold had expired — started a fresh order for Widget (TZS 50,000) "
        "and sent the prompt to 255722220001."
    )


async def test_resend_command_unknown_instagram_user(db_session, account, ig_account):
    assert (
        await handle_merchant_message(db_session, account, "/resend @ghost", "255700000920")
        == "No Instagram conversation with @ghost found."
    )


@respx.mock
async def test_resend_command_refires_instagram_order(
    db_session, customer, product, account, ig_account, ig_customer
):
    _mock_tunzaa()
    _order(db_session, customer, product, ig=(ig_account, ig_customer))

    reply = await handle_merchant_message(db_session, account, "/resend @resender", "255700000920")

    assert reply == "Payment prompt re-sent to @resender for Widget (TZS 50,000)."


async def test_resend_command_reports_throttle_wait(db_session, account, customer, product):
    order = _order(db_session, customer, product, account=account, updated_hours_ago=0)
    order.updated_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    reply = await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")

    assert "try again in" in reply
    assert reply.endswith("s.")


async def test_resend_command_instagram_without_connected_account(db_session, account):
    assert (
        await handle_merchant_message(db_session, account, "/resend @shopper", "255700000920")
        == "No Instagram account connected."
    )


async def test_resend_command_instagram_installment_only(
    db_session, customer, product, account, ig_account, ig_customer
):
    _order(db_session, customer, product, ig=(ig_account, ig_customer), payment_type=PaymentType.INSTALLMENT)

    reply = await handle_merchant_message(db_session, account, "/resend @resender", "255700000920")

    assert "installment plan" in reply


async def test_resend_command_instagram_no_pending_orders(db_session, account, ig_account, ig_customer):
    assert (
        await handle_merchant_message(db_session, account, "/resend @resender", "255700000920")
        == "No pending orders for @resender."
    )


async def test_resend_command_instagram_ambiguous_lists_candidates(
    db_session, owner, customer, product, account, ig_account, ig_customer
):
    other = Product(
        slug="resend-ig-gadget", name="Gadget", max_price=30000, min_price=25000,
        quantity=5, merchant_id=owner.id,
    )
    db_session.add(other)
    db_session.commit()
    _order(db_session, customer, product, ig=(ig_account, ig_customer))
    _order(db_session, customer, other, ig=(ig_account, ig_customer))

    reply = await handle_merchant_message(db_session, account, "/resend @resender", "255700000920")

    assert "more than one pending order" in reply
    assert "Widget — TZS 50,000" in reply


def test_render_resend_result_falls_back_without_detail(db_session, customer, product, account):
    from app.services.merchant_handler import _render_resend_result
    from app.services.payment_resend import ResendResult

    order = _order(db_session, customer, product, account=account)

    assert (
        _render_resend_result("255722220001", order, ResendResult(ResendOutcome.NOT_PENDING))
        == "This order can't be resent right now."
    )


async def test_resend_command_ignores_other_accounts_orders(db_session, account, customer, product, owner):
    stranger = User(username="255700000921")
    db_session.add(stranger)
    db_session.commit()
    other_account = WhatsAppAccount(
        name="Other", phone_number_id="OTHER_PHONE", access_token="tok", user_id=stranger.id,
    )
    db_session.add(other_account)
    db_session.commit()
    _order(db_session, customer, product, account=other_account)

    reply = await handle_merchant_message(db_session, account, "/resend 255722220001", "255700000920")

    assert reply == "No pending orders for 255722220001."


# --- POST /orders/{id}/resend ---


def _authed_merchant(db_session, auth_user):
    merchant, headers = auth_user
    acct = WhatsAppAccount(
        name="Resend API Shop", phone_number_id="RESEND_API_PHONE",
        access_token="tok", user_id=merchant.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return merchant, headers, acct


@respx.mock
def test_resend_endpoint_refires_live_order(client, db_session, auth_user, customer):
    _mock_tunzaa()
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["tunzaa_transaction_id"] == "TXN-RESEND-1"


def test_resend_endpoint_404_for_other_merchants_order(client, db_session, auth_user, owner, account, customer, product):
    _merchant, headers, _acct = _authed_merchant(db_session, auth_user)
    order = _order(db_session, customer, product, account=account)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 404


def test_resend_endpoint_400_for_settled_order(client, db_session, auth_user, customer):
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget2", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct, status=OrderStatus.PAID)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 400
    assert "not PENDING" in resp.json()["detail"]


def test_resend_endpoint_400_for_installment(client, db_session, auth_user, customer):
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget3", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct, payment_type=PaymentType.INSTALLMENT)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 400
    assert "Tunzaa's own schedule" in resp.json()["detail"]


def test_resend_endpoint_429_inside_throttle_window(client, db_session, auth_user, customer):
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget4", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct, updated_hours_ago=0)
    order.updated_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 429
    assert "try again in" in resp.json()["detail"]


def test_resend_endpoint_403_while_impersonating(client, db_session, auth_user, customer, monkeypatch):
    from app.core.config import settings

    merchant, _headers, acct = _authed_merchant(db_session, auth_user)
    operator = User(username="255700000999")
    db_session.add(operator)
    db_session.commit()
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", "255700000999")
    product = Product(
        slug="api-widget5", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct)
    token = support_access.create_impersonation_token(merchant.id, operator.id)

    resp = client.post(
        f"/api/v1/orders/{order.id}/resend", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 403


@respx.mock
def test_resend_endpoint_502_on_tunzaa_outage(client, db_session, auth_user, customer):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500, json={}))
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget7", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 502


@respx.mock
def test_resend_endpoint_500_when_order_vanishes_after_resend(
    client, db_session, auth_user, customer, monkeypatch
):
    _mock_tunzaa()
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget8", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    order = _order(db_session, customer, product, account=acct)
    monkeypatch.setattr("app.api.v1.orders.get_order_detail_for_merchant", lambda *a: None)

    resp = client.post(f"/api/v1/orders/{order.id}/resend", headers=headers)

    assert resp.status_code == 500
    assert "could not be reloaded" in resp.json()["detail"]


@respx.mock
def test_resend_endpoint_clones_expired_hold(client, db_session, auth_user, customer, monkeypatch):
    _closer(monkeypatch)
    _mock_tunzaa(txn_id="TXN-API-FRESH")
    merchant, headers, acct = _authed_merchant(db_session, auth_user)
    product = Product(
        slug="api-widget6", name="Widget", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
    )
    db_session.add(product)
    db_session.commit()
    old = _order(db_session, customer, product, account=acct, hold_hours=-3)

    resp = client.post(f"/api/v1/orders/{old.id}/resend", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] != str(old.id)
    assert body["tunzaa_transaction_id"] == "TXN-API-FRESH"
    db_session.refresh(old)
    assert old.status == OrderStatus.CANCELLED
