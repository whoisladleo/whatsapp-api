import pytest

from app.db.models import Product, User, WhatsAppAccount
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
