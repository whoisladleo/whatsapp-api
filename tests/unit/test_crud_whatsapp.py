from app.crud import whatsapp as crud_whatsapp
from app.db.models import User, WhatsAppAccount
from app.schemas.whatsapp import WhatsAppAccountCreate, WhatsAppAccountUpdate


def _user(db_session, username="255700000300"):
    user = User(username=username)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_create_whatsapp_account(db_session):
    user = _user(db_session)
    account_in = WhatsAppAccountCreate(
        name="Shop", phone_number_id="PH1", access_token="tok"
    )

    account = crud_whatsapp.create_whatsapp_account(db_session, account_in, user.id)

    assert account.id is not None
    assert account.phone_number_id == "PH1"
    assert account.user_id == user.id


def test_create_whatsapp_account_returns_none_on_duplicate_phone_number_id(db_session):
    user = _user(db_session)
    account_in = WhatsAppAccountCreate(
        name="Shop", phone_number_id="PH2", access_token="tok"
    )
    crud_whatsapp.create_whatsapp_account(db_session, account_in, user.id)

    duplicate = crud_whatsapp.create_whatsapp_account(db_session, account_in, user.id)

    assert duplicate is None


def test_get_account_by_phone_id_scoped_to_user(db_session):
    user = _user(db_session, "255700000301")
    other = _user(db_session, "255700000302")
    account_in = WhatsAppAccountCreate(name="Shop", phone_number_id="PH3", access_token="tok")
    crud_whatsapp.create_whatsapp_account(db_session, account_in, user.id)

    found = crud_whatsapp.get_account_by_phone_id(db_session, "PH3", user.id)
    not_found = crud_whatsapp.get_account_by_phone_id(db_session, "PH3", other.id)

    assert found is not None
    assert not_found is None


def test_update_whatsapp_account(db_session):
    user = _user(db_session, "255700000303")
    account_in = WhatsAppAccountCreate(name="Shop", phone_number_id="PH4", access_token="tok")
    account = crud_whatsapp.create_whatsapp_account(db_session, account_in, user.id)

    updated = crud_whatsapp.update_whatsapp_account(
        db_session, account, WhatsAppAccountUpdate(name="New Name", ai_enabled=True)
    )

    assert updated.name == "New Name"
    assert updated.ai_enabled is True
    assert updated.phone_number_id == "PH4"


def test_get_multi_by_user_respects_scope_and_pagination(db_session):
    user = _user(db_session, "255700000304")
    other = _user(db_session, "255700000305")
    for i in range(3):
        crud_whatsapp.create_whatsapp_account(
            db_session,
            WhatsAppAccountCreate(name=f"Shop{i}", phone_number_id=f"PHU{i}", access_token="tok"),
            user.id,
        )
    crud_whatsapp.create_whatsapp_account(
        db_session,
        WhatsAppAccountCreate(name="Theirs", phone_number_id="PHO0", access_token="tok"),
        other.id,
    )

    all_for_user = crud_whatsapp.get_multi_by_user(db_session, user.id)
    limited = crud_whatsapp.get_multi_by_user(db_session, user.id, skip=0, limit=1)

    assert len(all_for_user) == 3
    assert len(limited) == 1


def test_delete_whatsapp_account(db_session):
    user = _user(db_session, "255700000306")
    account = crud_whatsapp.create_whatsapp_account(
        db_session,
        WhatsAppAccountCreate(name="Shop", phone_number_id="PH5", access_token="tok"),
        user.id,
    )
    account_id = account.id

    crud_whatsapp.delete_whatsapp_account(db_session, account)

    assert db_session.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_id).first() is None
