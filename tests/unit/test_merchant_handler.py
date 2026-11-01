import json

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
    Product,
    User,
    WhatsAppAccount,
)
from app.services.merchant_handler import handle_merchant_message

WHATSAPP_MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
INSTAGRAM_MESSAGES_URL_RE = r"https://graph\.instagram\.com/.+/messages$"


@pytest.fixture
def merchant_account(db_session):
    owner = User(username="255700000099")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)

    product = Product(
        slug="shoes",
        name="Shoes",
        max_price=50000,
        min_price=40000,
        quantity=5,
        merchant_id=owner.id,
    )
    db_session.add(product)

    account = WhatsAppAccount(
        name="Test Merchant",
        phone_number_id="PHONE1",
        access_token="tok",
        user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    db_session.refresh(product)

    return account


@pytest.mark.parametrize("body", ["/stock", "/price", "/manage"])
async def test_bare_command_enters_management_mode(db_session, merchant_account, body):
    reply = await handle_merchant_message(db_session, merchant_account, body, "255700000099")

    assert "management mode" in reply.lower()


async def test_stock_increment(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes +10", "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert product.quantity == 15
    assert "Updated stock" in reply
    assert "15" in reply


async def test_stock_decrement(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes -3", "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert product.quantity == 2
    assert "Updated stock" in reply


async def test_stock_invalid_format(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes", "255700000099"
    )

    assert "Invalid format" in reply


async def test_stock_product_not_found(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Hats +5", "255700000099"
    )

    assert "not found" in reply


async def test_stock_decrement_clamps_at_zero(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes -99", "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert product.quantity == 0
    assert "New quantity: 0" in reply


async def test_stock_none_quantity_treated_as_zero(db_session, merchant_account):
    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    product.quantity = None
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes +5", "255700000099"
    )

    db_session.refresh(product)
    assert product.quantity == 5
    assert "New quantity: 5" in reply


async def test_out_of_stock(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/out_of_stock Shoes", "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert product.quantity == 0
    assert "out of stock" in reply.lower()


async def test_out_of_stock_product_not_found(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/out_of_stock Hats", "255700000099"
    )

    assert "not found" in reply


async def test_price_update(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/price Shoes max:60000 min:45000", "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert product.max_price == 60000
    assert product.min_price == 45000
    assert "Updated prices" in reply


async def test_price_invalid_format(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/price Shoes max:60000", "255700000099"
    )

    assert "Invalid format" in reply


async def test_price_product_not_found(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/price Hats max:1 min:1", "255700000099"
    )

    assert "not found" in reply


@pytest.mark.parametrize("command", [
    "/price Shoes max:40000 min:50000",
    "/price Shoes max:50000 min:0",
    "/price Shoes max:0 min:0",
])
async def test_price_rejected_unless_zero_lt_min_lte_max(db_session, merchant_account, command):
    reply = await handle_merchant_message(
        db_session, merchant_account, command, "255700000099"
    )

    product = db_session.query(Product).filter(Product.slug == "shoes").first()
    assert "need 0 < min <= max" in reply
    assert (product.max_price, product.min_price) == (50000, 40000)


async def test_unknown_command_falls_back_to_help(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "hello there", "255700000099"
    )

    assert "Unknown command" in reply


async def test_stock_lookup_is_scoped_to_merchant(db_session, merchant_account):
    other_owner = User(username="255799999999")
    db_session.add(other_owner)
    db_session.commit()
    db_session.refresh(other_owner)

    other_product = Product(
        slug="bags", name="Bags", max_price=1000, min_price=500,
        quantity=1, merchant_id=other_owner.id,
    )
    db_session.add(other_product)
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Bags +1", "255700000099"
    )

    assert "not found" in reply


async def _handed_off_customer(db_session, account, phone="255722223333"):
    customer = Customer(phone_number=phone)
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    db_session.add(
        Message(
            from_phone=phone, type="text", body="need help",
            direction="inbound", status="sent", account_id=account.id,
        )
    )
    from datetime import datetime

    context = ConversationContext(
        customer_id=customer.id,
        state=ConversationState.HUMAN_HANDOFF,
        handoff_at=datetime.utcnow(),
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return customer, context


async def test_bare_resume_with_nothing_waiting(db_session, merchant_account):
    reply = await handle_merchant_message(db_session, merchant_account, "/resume", "255700000099")

    assert reply == "No customers are waiting for you right now."


async def _ig_handed_off_customer(db_session, merchant_account, username="waiter", scoped_id="igsid_waiter"):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_list", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)
    ig_customer = InstagramCustomer(
        ig_scoped_id=scoped_id, username=username, account_id=ig_account.id,
    )
    db_session.add(ig_customer)
    db_session.commit()
    db_session.refresh(ig_customer)
    from datetime import datetime

    context = InstagramConversationContext(
        customer_id=ig_customer.id, state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)
    return ig_customer, context


async def test_bare_resume_lists_pending_handoffs(db_session, merchant_account):
    wa_customer, _ = await _handed_off_customer(db_session, merchant_account)
    await _ig_handed_off_customer(db_session, merchant_account)

    reply = await handle_merchant_message(db_session, merchant_account, "/resume", "255700000099")

    assert "1. " in reply and "2. " in reply
    assert wa_customer.phone_number in reply
    assert "@waiter" in reply
    assert "/resume <number above>" in reply


async def test_resume_index_pick_whatsapp(db_session, merchant_account):
    customer, context = await _handed_off_customer(db_session, merchant_account)

    reply = await handle_merchant_message(db_session, merchant_account, "/resume 1", "255700000099")

    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert f"Conversation with {customer.phone_number} resumed" in reply


async def test_resume_index_pick_instagram(db_session, merchant_account):
    _, context = await _ig_handed_off_customer(db_session, merchant_account)

    reply = await handle_merchant_message(db_session, merchant_account, "/resume 1", "255700000099")

    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert "Conversation with @waiter resumed" in reply


async def test_resume_out_of_range_index_falls_through_to_phone_path(db_session, merchant_account):
    await _handed_off_customer(db_session, merchant_account)

    reply = await handle_merchant_message(db_session, merchant_account, "/resume 9", "255700000099")

    assert "No conversation found for 9." in reply


async def test_resume_unknown_customer(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume 255700000001", "255700000099"
    )

    assert "No conversation found" in reply


async def test_resume_customer_with_no_history_on_this_account(db_session, merchant_account):
    customer = Customer(phone_number="255722224444")
    db_session.add(customer)
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume 255722224444", "255700000099"
    )

    assert "No conversation with 255722224444 found for this account" in reply


async def test_resume_customer_not_in_handoff(db_session, merchant_account):
    customer = Customer(phone_number="255722225555")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        Message(
            from_phone="255722225555", type="text", body="hi",
            direction="inbound", status="sent", account_id=merchant_account.id,
        )
    )
    db_session.add(ConversationContext(customer_id=customer.id, state=ConversationState.NEGOTIATING))
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume 255722225555", "255700000099"
    )

    assert "not currently in a human handoff state" in reply


async def test_resume_success(db_session, merchant_account):
    customer, context = await _handed_off_customer(db_session, merchant_account)

    reply = await handle_merchant_message(
        db_session, merchant_account, f"/resume {customer.phone_number}", "255700000099"
    )

    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert context.handoff_at is None
    assert "resumed" in reply.lower()


async def test_resume_instagram_no_account_connected(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume @some_customer", "255700000099"
    )

    assert "No Instagram account connected" in reply


async def test_resume_instagram_unknown_customer(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume @nobody", "255700000099"
    )

    assert "No Instagram conversation with @nobody found" in reply


async def test_resume_instagram_customer_not_in_handoff(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)

    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_2", username="commenter2", account_id=ig_account.id,
    )
    db_session.add(ig_customer)
    db_session.commit()
    db_session.refresh(ig_customer)
    db_session.add(
        InstagramConversationContext(customer_id=ig_customer.id, state=ConversationState.NEGOTIATING)
    )
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume @commenter2", "255700000099"
    )

    assert "@commenter2 is not currently in a human handoff state" in reply


async def test_resume_instagram_success(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)

    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_3", username="commenter3", account_id=ig_account.id,
    )
    db_session.add(ig_customer)
    db_session.commit()
    db_session.refresh(ig_customer)

    from datetime import datetime

    context = InstagramConversationContext(
        customer_id=ig_customer.id, state=ConversationState.HUMAN_HANDOFF, handoff_at=datetime.utcnow(),
    )
    db_session.add(context)
    db_session.commit()
    db_session.refresh(context)

    reply = await handle_merchant_message(
        db_session, merchant_account, "/resume @commenter3", "255700000099"
    )

    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert context.handoff_at is None
    assert "Conversation with @commenter3 resumed" in reply


# --- /reply --------------------------------------------------------------


async def test_reply_missing_args_shows_usage(db_session, merchant_account):
    reply = await handle_merchant_message(db_session, merchant_account, "/reply", "255700000099")

    assert "Usage" in reply


async def test_reply_missing_message_shows_usage(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply 255722223333", "255700000099"
    )

    assert "Usage" in reply


@respx.mock
async def test_reply_whatsapp_customer_no_history_is_rejected(db_session, merchant_account):
    route = respx.post(url__regex=WHATSAPP_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
    )

    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply 255722229999 Hello there", "255700000099"
    )

    assert "No conversation with 255722229999 found for this account" in reply
    assert not route.called


@respx.mock
async def test_reply_whatsapp_customer_success(db_session, merchant_account):
    customer, context = await _handed_off_customer(db_session, merchant_account)
    route = respx.post(url__regex=WHATSAPP_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.reply1"}]})
    )

    reply = await handle_merchant_message(
        db_session, merchant_account, f"/reply {customer.phone_number} We'll ship it today.", "255700000099"
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["to"] == customer.phone_number
    assert sent["text"]["body"] == "We'll ship it today."
    assert f"Sent to {customer.phone_number}" in reply

    outbound = db_session.query(Message).filter(
        Message.to_phone == customer.phone_number, Message.direction == "outbound"
    ).first()
    assert outbound is not None
    assert outbound.body == "We'll ship it today."
    assert outbound.whatsapp_message_id == "wamid.reply1"


@respx.mock
async def test_reply_whatsapp_customer_meta_error_is_reported(db_session, merchant_account):
    customer, _ = await _handed_off_customer(db_session, merchant_account)
    respx.post(url__regex=WHATSAPP_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "recipient not on WhatsApp"}})
    )

    reply = await handle_merchant_message(
        db_session, merchant_account, f"/reply {customer.phone_number} hi", "255700000099"
    )

    assert "Failed to send reply" in reply
    assert "recipient not on WhatsApp" in reply


async def test_reply_instagram_no_account_connected(db_session, merchant_account):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply @some_customer hi there", "255700000099"
    )

    assert "No Instagram account connected" in reply


async def test_reply_instagram_unknown_customer(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()

    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply @nobody hi there", "255700000099"
    )

    assert "No Instagram conversation with @nobody found" in reply


@respx.mock
async def test_reply_instagram_customer_success(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)

    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_1", username="commenter1", account_id=ig_account.id,
    )
    db_session.add(ig_customer)
    db_session.commit()

    route = respx.post(url__regex=INSTAGRAM_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"recipient_id": "igsid_1", "message_id": "ig_msg_1"})
    )

    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply @commenter1 It's back in stock!", "255700000099"
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["recipient"] == {"id": "igsid_1"}
    assert sent["message"] == {"text": "It's back in stock!"}
    # A merchant's /reply is a genuine human agent response — see #13.
    assert sent["tag"] == "HUMAN_AGENT"
    assert "Sent to @commenter1" in reply

    from app.db.models import InstagramDirectMessage

    outbound = db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.customer_id == ig_customer.id, InstagramDirectMessage.direction == "outbound"
    ).first()
    assert outbound is not None
    assert outbound.body == "It's back in stock!"
    assert outbound.ig_message_id == "ig_msg_1"


@respx.mock
async def test_reply_instagram_customer_meta_error_is_reported(db_session, merchant_account):
    ig_account = InstagramAccount(
        ig_business_id="ig_biz_1", access_token="ig-tok", username="shop",
        user_id=merchant_account.user_id,
    )
    db_session.add(ig_account)
    db_session.commit()
    db_session.refresh(ig_account)

    ig_customer = InstagramCustomer(
        ig_scoped_id="igsid_1", username="commenter1", account_id=ig_account.id,
    )
    db_session.add(ig_customer)
    db_session.commit()

    respx.post(url__regex=INSTAGRAM_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "user not reachable"}})
    )

    reply = await handle_merchant_message(
        db_session, merchant_account, "/reply @commenter1 hi", "255700000099"
    )

    assert "Failed to send reply" in reply
    assert "user not reachable" in reply


async def test_cancel_with_armed_reply_mode_disarms(db_session, merchant_account, monkeypatch):
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())
    flow_module.arm_reply_mode(
        merchant_account.user_id,
        {"channel": "whatsapp", "ref": "255711111111", "name": "255711111111"},
    )

    reply = await handle_merchant_message(db_session, merchant_account, "/cancel", "255700000099")

    assert reply == "Cancelled — nothing was sent."
    assert flow_module.get_reply_mode(merchant_account.user_id) is None


async def test_cancel_with_nothing_armed(db_session, merchant_account, monkeypatch):
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())

    reply = await handle_merchant_message(db_session, merchant_account, "/cancel", "255700000099")

    assert reply == "Nothing to cancel."
