import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Query

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


def test_lock_merchant_row_returns_merchant(db_session):
    user = User(username="255700000702")
    db_session.add(user)
    db_session.commit()

    locked = crud_product.lock_merchant_row(db_session, user.id)

    assert locked.id == user.id


def test_lock_merchant_row_missing_user_raises(db_session):
    from sqlalchemy.exc import NoResultFound

    with pytest.raises(NoResultFound):
        crud_product.lock_merchant_row(db_session, uuid.uuid4())


def test_import_catalog_products_holds_merchant_lock(db_session, monkeypatch):
    """
    Regression test for the simultaneous-import race: two imports for one
    merchant must serialize on the merchant row, or both miss each other's
    uncommitted rows and insert duplicates (no DB uniqueness on sku/name).
    A real threaded race is untestable here — the suite runs on one
    StaticPool :memory: SQLite connection — so this pins the wiring
    instead: every import requests the row lock exactly once.
    """
    user = User(username="255700000703")
    db_session.add(user)
    db_session.commit()

    calls = []
    real_with_for_update = Query.with_for_update

    def recording_with_for_update(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_with_for_update(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", recording_with_for_update)

    stats = crud_product.import_catalog_products(
        db_session,
        user.id,
        [{"name": "Kettle", "max_price": 1.0, "min_price": 1.0, "sku": "igshop:1"}],
    )

    assert stats["imported_count"] == 1
    assert len(calls) == 1


def test_merchant_lock_renders_for_update_on_postgres_only():
    """
    Pins the dialect assumption the import guard rests on: Postgres honors
    the row lock, SQLite silently renders a plain SELECT (which is why the
    test above spies the method call instead of sniffing SQL, and why
    SQLite correctness comes from its database-level writer serialization).
    """
    stmt = select(User.id).filter(User.id == uuid.uuid4()).with_for_update()

    pg_sql = str(stmt.compile(dialect=postgresql.dialect()))
    sqlite_sql = str(stmt.compile(dialect=sqlite.dialect()))

    assert "FOR UPDATE" in pg_sql
    assert "FOR UPDATE" not in sqlite_sql
