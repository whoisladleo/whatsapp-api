import pytest

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Message,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.merchant_handler import handle_merchant_message


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


async def test_resume_missing_phone_shows_usage(db_session, merchant_account):
    reply = await handle_merchant_message(db_session, merchant_account, "/resume", "255700000099")

    assert "Usage" in reply


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
