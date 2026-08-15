from app.crud import account as crud_account
from app.crud import message as crud_message
from app.crud import product as crud_product
from app.crud import user as crud_user
from app.db.models import Message, User, WhatsAppAccount


def test_crud_user_get_user(db_session):
    user = User(username="255700000700")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    found = crud_user.get_user(db_session, user.id)
    missing = crud_user.get_user(db_session, 999999)

    assert found.id == user.id
    assert missing is None


def test_crud_account_delete_account(db_session):
    user = User(username="255700000701")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    account = WhatsAppAccount(
        name="Shop", phone_number_id="MISC_PH1", access_token="tok", user_id=user.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    account_id = account.id

    crud_account.delete_account(db_session, account)

    assert db_session.query(WhatsAppAccount).filter(
        WhatsAppAccount.id == account_id
    ).first() is None


def test_crud_message_create_message_dedupes_by_whatsapp_message_id(db_session):
    user = User(username="255700000702")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    account = WhatsAppAccount(
        name="Shop", phone_number_id="MISC_PH2", access_token="tok", user_id=user.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    data = {
        "whatsapp_message_id": "wamid.dupe1",
        "from_phone": "255711110000",
        "type": "text",
        "body": "hi",
        "direction": "inbound",
    }
    first = crud_message.create_message(db_session, data, account.id)
    second = crud_message.create_message(db_session, data, account.id)

    assert first.id == second.id
    assert db_session.query(Message).count() == 1


def test_generate_slug_falls_back_to_uuid_for_unusable_name(db_session):
    slug = crud_product.generate_slug(db_session, "!!!")

    assert slug != ""
    assert len(slug) == 8
