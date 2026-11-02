from datetime import datetime

import fakeredis
import pytest

import app.services.handoff_flow as flow_module
from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Message,
    User,
    WhatsAppAccount,
)
from app.services.handoff_flow import consume_directive_ig, consume_directive_wa
from app.services.llm import LLMService
from app.services.merchant_handler import handle_merchant_message


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


class _FakeContentBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContentBlock(text=text)]


def _service_with_fake_client(handler=None, captured=None, reply_text="Karibu! How can I help?"):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse(reply_text)

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000111")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Test Shop", phone_number_id="DIRECT_PHONE1", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def other_account(db_session):
    owner = User(username="255700000222")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    account = WhatsAppAccount(
        name="Other Shop", phone_number_id="DIRECT_PHONE2", access_token="tok",
        user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def ig_setup(db_session, merchant):
    account = InstagramAccount(
        ig_business_id="ig_biz_direct", access_token="ig-tok", username="shop",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    customer = InstagramCustomer(
        ig_scoped_id="igsid_direct", username="buyer1", account_id=account.id
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = InstagramConversationContext(customer_id=customer.id)
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return account, customer, context


def _add_history(db_session, account, phone):
    db_session.add(
        Message(
            from_phone=phone, type="text", body="need help",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    db_session.commit()


def _customer_with_history(db_session, account, phone):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    _add_history(db_session, account, phone)
    return customer


def _hand_off_wa(db_session, customer):
    db_session.add(
        ConversationContext(
            customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()


# --- /direct parsing -----------------------------------------------------


async def test_direct_bare_shows_usage(db_session, shop_account):
    reply = await handle_merchant_message(db_session, shop_account, "/direct", "255700000111")

    assert "Usage" in reply
    assert "/direct" in reply


async def test_direct_missing_instruction_shows_usage(db_session, shop_account):
    customer = _customer_with_history(db_session, shop_account, "255722223331")
    reply = await handle_merchant_message(
        db_session, shop_account, f"/direct {customer.phone_number}", "255700000111"
    )

    assert "Usage" in reply


async def test_direct_whatsapp_success_echoes_instruction(db_session, shop_account):
    customer = _customer_with_history(db_session, shop_account, "255722223332")

    reply = await handle_merchant_message(
        db_session, shop_account,
        f"/direct {customer.phone_number} Offer free delivery today",
        "255700000111",
    )

    assert "255722223332" in reply
    assert "Offer free delivery today" in reply
    assert consume_directive_wa(shop_account.id, customer.id) == "Offer free delivery today"
    # One-shot: already consumed by the check above.
    assert consume_directive_wa(shop_account.id, customer.id) is None


async def test_direct_whatsapp_unknown_number_is_rejected(db_session, shop_account):
    reply = await handle_merchant_message(
        db_session, shop_account, "/direct 255722229999 Be nice", "255700000111"
    )

    assert "No conversation with 255722229999 found for this account" in reply


async def test_direct_pending_index_resolves_whatsapp(db_session, shop_account):
    customer = _customer_with_history(db_session, shop_account, "255722223333")
    _hand_off_wa(db_session, customer)

    reply = await handle_merchant_message(
        db_session, shop_account, "/direct 1 Mention the warranty", "255700000111"
    )

    assert "255722223333" in reply
    assert "Mention the warranty" in reply
    assert consume_directive_wa(shop_account.id, customer.id) == "Mention the warranty"


async def test_direct_out_of_range_index_falls_through_to_phone(db_session, shop_account):
    customer = _customer_with_history(db_session, shop_account, "255722223334")
    _hand_off_wa(db_session, customer)

    reply = await handle_merchant_message(
        db_session, shop_account, "/direct 9 Some instruction", "255700000111"
    )

    assert "No conversation with 9 found for this account" in reply


async def test_direct_pending_index_resolves_instagram(db_session, shop_account, ig_setup):
    _, ig_customer, ig_context = ig_setup
    ig_context.state = ConversationState.HUMAN_HANDOFF
    ig_context.handoff_at = datetime.utcnow()
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, shop_account, "/direct 1 Ask for their phone number", "255700000111"
    )

    assert "@buyer1" in reply
    assert "Ask for their phone number" in reply
    assert consume_directive_ig(ig_customer.id) == "Ask for their phone number"


async def test_direct_instagram_success(db_session, shop_account, ig_setup):
    _, ig_customer, _ = ig_setup

    reply = await handle_merchant_message(
        db_session, shop_account, "/direct @buyer1 Confirm the size first", "255700000111"
    )

    assert "@buyer1" in reply
    assert "Confirm the size first" in reply
    assert consume_directive_ig(ig_customer.id) == "Confirm the size first"


async def test_direct_instagram_no_account_connected(db_session, shop_account):
    reply = await handle_merchant_message(
        db_session, shop_account, "/direct @buyer1 hi", "255700000111"
    )

    assert "No Instagram account connected" in reply


async def test_direct_instagram_unknown_customer(db_session, shop_account, ig_setup):
    reply = await handle_merchant_message(
        db_session, shop_account, "/direct @nobody hi", "255700000111"
    )

    assert "No Instagram conversation with @nobody found" in reply


async def test_manage_help_mentions_direct(db_session, shop_account):
    reply = await handle_merchant_message(db_session, shop_account, "/manage", "255700000111")

    assert "/direct 2557XXXXXXXX Your instruction" in reply
    assert "/direct @their_instagram_username Your instruction" in reply


# --- injection: steer-once-then-clear, scoping, no-leak --------------------


async def test_steer_once_then_clear_wa(db_session, shop_account):
    phone = "255722223335"
    _customer_with_history(db_session, shop_account, phone)
    await handle_merchant_message(
        db_session, shop_account, f"/direct {phone} STEER_ALPHA Once-only offer", "255700000111"
    )

    first_captured, second_captured = {}, {}
    first = await _service_with_fake_client(captured=first_captured).generate_response(
        message_body="hi", from_phone=phone, db=db_session, account=shop_account,
    )
    second = await _service_with_fake_client(captured=second_captured).generate_response(
        message_body="hi again", from_phone=phone, db=db_session, account=shop_account,
    )

    assert "STEER_ALPHA Once-only offer" in first_captured["system"]
    assert "STEER_ALPHA" not in second_captured["system"]
    assert first is not None and second is not None


async def test_directive_cross_merchant_scoping(db_session, shop_account, other_account):
    phone = "255722223336"
    customer = _customer_with_history(db_session, shop_account, phone)
    _add_history(db_session, other_account, phone)
    await handle_merchant_message(
        db_session, shop_account, f"/direct {phone} STEER_SCOPED Shop A only", "255700000111"
    )

    other_captured, own_captured = {}, {}
    await _service_with_fake_client(captured=other_captured).generate_response(
        message_body="hi", from_phone=phone, db=db_session, account=other_account,
    )
    await _service_with_fake_client(captured=own_captured).generate_response(
        message_body="hi", from_phone=phone, db=db_session, account=shop_account,
    )

    assert "STEER_SCOPED" not in other_captured["system"]
    assert "STEER_SCOPED Shop A only" in own_captured["system"]
    assert customer is not None  # shared global Customer row; keys still differ by account


async def test_directive_never_leaks_to_customer(db_session, shop_account):
    phone = "255722223337"
    _customer_with_history(db_session, shop_account, phone)
    await handle_merchant_message(
        db_session, shop_account, f"/direct {phone} STEER_SECRET internal margin note",
        "255700000111",
    )

    captured = {}
    reply = await _service_with_fake_client(captured=captured).generate_response(
        message_body="hi", from_phone=phone, db=db_session, account=shop_account,
    )

    assert "STEER_SECRET internal margin note" in captured["system"]
    assert "STEER_SECRET" not in reply
    assert "Operator steering" not in reply


async def test_steer_once_then_clear_ig(db_session, shop_account, ig_setup):
    ig_account, ig_customer, ig_context = ig_setup
    await handle_merchant_message(
        db_session, shop_account, "/direct @buyer1 STEER_IG_DM Ask about budget",
        "255700000111",
    )

    first_captured, second_captured = {}, {}
    first = await _service_with_fake_client(captured=first_captured).generate_dm_response(
        "hi", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )
    second = await _service_with_fake_client(captured=second_captured).generate_dm_response(
        "hi again", db=db_session, account=ig_account, customer=ig_customer,
        context=ig_context, conversation_history=[],
    )

    assert "STEER_IG_DM Ask about budget" in first_captured["system"]
    assert "STEER_IG_DM" not in second_captured["system"]
    assert "STEER_IG_DM" not in first
    assert second is not None


async def test_directive_survives_handed_off_turn(db_session, shop_account):
    phone = "255722223338"
    customer = _customer_with_history(db_session, shop_account, phone)
    _hand_off_wa(db_session, customer)
    await handle_merchant_message(
        db_session, shop_account, f"/direct {phone} STEER_PAUSED wait for resume",
        "255700000111",
    )

    reply = await _service_with_fake_client().generate_response(
        message_body="hi", from_phone=phone, db=db_session, account=shop_account,
    )

    assert reply is None  # still handed off — nothing generated
    assert consume_directive_wa(shop_account.id, customer.id) == "STEER_PAUSED wait for resume"
