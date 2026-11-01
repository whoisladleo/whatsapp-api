"""Encryption at rest for stored Meta tokens and number PINs (finding 9)."""

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import text

from app.db.encrypted import decrypt_str
from app.db.models import PhoneNumberPool, WhatsAppAccount


def _raw_token(db_session):
    return db_session.execute(text("SELECT access_token FROM whatsapp_accounts")).scalar()


def test_token_round_trips_through_orm_as_plaintext(db_session):
    account = WhatsAppAccount(
        name="shop", phone_number_id="pn_1", access_token="meta-secret-token"
    )
    db_session.add(account)
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.query(WhatsAppAccount).first()
    assert reloaded.access_token == "meta-secret-token"


def test_token_is_ciphertext_at_rest(db_session):
    db_session.add(
        WhatsAppAccount(name="shop", phone_number_id="pn_1", access_token="meta-secret-token")
    )
    db_session.commit()

    stored = _raw_token(db_session)
    assert stored != "meta-secret-token"
    assert "meta-secret-token" not in stored
    assert decrypt_str(stored) == "meta-secret-token"


def test_identical_tokens_store_differently(db_session):
    db_session.add_all(
        [
            WhatsAppAccount(name="a", phone_number_id="pn_1", access_token="same-token"),
            WhatsAppAccount(name="b", phone_number_id="pn_2", access_token="same-token"),
        ]
    )
    db_session.commit()

    stored = db_session.execute(text("SELECT access_token FROM whatsapp_accounts")).scalars().all()
    assert len(set(stored)) == 2


def test_pin_none_round_trips_as_none(db_session):
    db_session.add(PhoneNumberPool(phone_number_id="pn_1", two_step_pin=None))
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.query(PhoneNumberPool).first()
    assert reloaded.two_step_pin is None


def test_pin_is_ciphertext_at_rest(db_session):
    db_session.add(PhoneNumberPool(phone_number_id="pn_1", two_step_pin="123456"))
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.query(PhoneNumberPool).first()
    assert reloaded.two_step_pin == "123456"
    stored = db_session.execute(text("SELECT two_step_pin FROM phone_number_pool")).scalar()
    assert stored != "123456"
    assert decrypt_str(stored) == "123456"


def test_tampered_ciphertext_fails_loud_on_read(db_session):
    db_session.add(
        WhatsAppAccount(name="shop", phone_number_id="pn_1", access_token="meta-secret-token")
    )
    db_session.commit()
    db_session.execute(text("UPDATE whatsapp_accounts SET access_token = 'not-a-fernet-token'"))
    db_session.commit()
    db_session.expire_all()

    with pytest.raises(InvalidToken):
        db_session.query(WhatsAppAccount).first()
