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


def test_crud_user_otp_round_trip_hashes_and_verifies(db_session):
    from datetime import datetime, timedelta

    user = User(username="255700000702")
    db_session.add(user)
    db_session.commit()

    crud_user.set_user_otp(
        db_session, user, "123456", datetime.utcnow() + timedelta(minutes=5)
    )

    assert user.otp_code != "123456"
    assert crud_user.verify_user_otp(user, "123456") is True
    assert crud_user.verify_user_otp(user, "000000") is False

    crud_user.clear_user_otp(db_session, user)
    assert crud_user.verify_user_otp(user, "123456") is False


def test_crud_refresh_token_create_get_revoke(db_session):
    import hashlib
    from datetime import datetime, timedelta

    from app.crud import refresh_token as crud_refresh

    user = User(username="255700000703")
    db_session.add(user)
    db_session.commit()

    token_hash = hashlib.sha256(b"raw-bearer").hexdigest()
    record = crud_refresh.create_refresh_token(
        db_session, user.id, token_hash, datetime.utcnow() + timedelta(days=7)
    )

    assert crud_refresh.get_refresh_token_by_hash(db_session, token_hash).id == record.id
    assert crud_refresh.get_refresh_token_by_hash(db_session, "nope") is None

    crud_refresh.revoke_refresh_token(db_session, record)
    assert record.revoked_at is not None


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


def _draft_product(db_session, merchant_id, **overrides):
    from app.db.models import Product

    attrs = {
        "slug": f"draft-{uuid.uuid4().hex[:6]}",
        "name": "Untitled import 98765432",
        "max_price": 0.0,
        "min_price": 0.0,
        "quantity": 0,
        "is_active": False,
        "is_draft": True,
        "merchant_id": merchant_id,
    }
    attrs.update(overrides)
    p = Product(**attrs)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_maybe_complete_draft_completes_valid_save(db_session):
    user = User(username="255700000704")
    db_session.add(user)
    db_session.commit()
    p = _draft_product(db_session, user.id)
    p.name = "Leather Handbag"
    p.max_price = 120000.0
    p.min_price = 100000.0

    assert crud_product.maybe_complete_draft(p) is True
    assert p.is_draft is False
    assert p.is_active is True
    # Quantity untouched: still out of stock until the merchant sets units.
    assert p.quantity == 0


def test_maybe_complete_draft_stays_until_fully_valid(db_session):
    user = User(username="255700000705")
    db_session.add(user)
    db_session.commit()

    # Placeholder name kept, even with prices set.
    p = _draft_product(db_session, user.id)
    p.max_price = 120000.0
    p.min_price = 100000.0
    assert crud_product.maybe_complete_draft(p) is False
    assert p.is_draft is True

    # Real name but zero prices.
    p.name = "Leather Handbag"
    p.max_price = 0.0
    assert crud_product.maybe_complete_draft(p) is False
    assert p.is_draft is True
    assert p.is_active is False


def test_maybe_complete_draft_ignores_non_drafts(db_session):
    user = User(username="255700000706")
    db_session.add(user)
    db_session.commit()
    p = _draft_product(
        db_session, user.id, name="Retired", max_price=10.0, min_price=5.0,
        is_draft=False, is_active=False,
    )

    # An inactive non-draft is a merchant choice — never touched.
    assert crud_product.maybe_complete_draft(p) is False
    assert p.is_active is False


def test_bulk_upsert_cannot_flip_is_draft_directly(db_session):
    from app.db.models import Product

    user = User(username="255700000707")
    db_session.add(user)
    db_session.commit()
    p = _draft_product(db_session, user.id, sku="D-1")

    stats = crud_product.bulk_upsert_products(
        db_session, user.id,
        [{"sku": "D-1", "name": "Hacked", "is_draft": False}],
    )

    db_session.refresh(p)
    assert p.is_draft is True
    assert p.name == "Hacked"
    assert stats["duplicate_products_count"] == 1


def test_bulk_upsert_completes_draft_via_valid_row(db_session):
    user = User(username="255700000708")
    db_session.add(user)
    db_session.commit()
    p = _draft_product(db_session, user.id, sku="D-2")

    crud_product.bulk_upsert_products(
        db_session, user.id,
        [{
            "sku": "D-2", "name": "Leather Handbag",
            "max_price": 120000.0, "min_price": 100000.0,
        }],
    )

    db_session.refresh(p)
    assert p.is_draft is False
    assert p.is_active is True


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


def test_create_product_retries_slug_on_integrity_error(db_session, monkeypatch):
    """
    generate_slug is check-then-insert: a concurrent create can commit the
    same slug between our check and our flush. The create must retry once
    with a fresh suffix instead of 500ing on the IntegrityError.
    """
    from app.db.models import Product
    from app.schemas.product import ProductCreate

    user = User(username="255700000709")
    db_session.add(user)
    db_session.commit()
    crud_product.create_product(
        db_session,
        ProductCreate(name="Shoes", max_price=10, min_price=5),
        user.id,
    )

    # Force every slug check to collide with the committed row.
    monkeypatch.setattr(crud_product, "generate_slug", lambda db, name: "shoes")

    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Other", max_price=10, min_price=5),
        user.id,
    )

    assert created.slug != "shoes"
    assert created.slug.startswith("shoes-")
    assert db_session.query(Product).count() == 2


def test_create_product_raises_after_single_slug_retry(db_session, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.schemas.product import ProductCreate

    user = User(username="255700000710")
    db_session.add(user)
    db_session.commit()

    calls = []

    def failing_commit():
        calls.append(1)
        raise IntegrityError("INSERT", {}, Exception("boom"))

    monkeypatch.setattr(db_session, "commit", failing_commit)

    with pytest.raises(IntegrityError):
        crud_product.create_product(
            db_session,
            ProductCreate(name="Shoes", max_price=10, min_price=5),
            user.id,
        )

    # One initial attempt plus exactly one retry.
    assert len(calls) == 2


def test_bulk_upsert_products_holds_merchant_lock(db_session, monkeypatch):
    """
    Same wiring pin as the import-lock test above: simultaneous bulk
    uploads for one merchant must serialize on the merchant row, or both
    miss each other's uncommitted sku rows and insert duplicates.
    """
    user = User(username="255700000711")
    db_session.add(user)
    db_session.commit()

    calls = []
    real_with_for_update = Query.with_for_update

    def recording_with_for_update(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_with_for_update(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", recording_with_for_update)

    crud_product.bulk_upsert_products(
        db_session,
        user.id,
        [{"name": "Kettle", "max_price": 1.0, "min_price": 1.0, "sku": "K-1"}],
    )

    assert len(calls) == 1


def test_bulk_upsert_dry_run_skips_merchant_lock(db_session, monkeypatch):
    user = User(username="255700000712")
    db_session.add(user)
    db_session.commit()

    calls = []
    real_with_for_update = Query.with_for_update

    def recording_with_for_update(self, *args, **kwargs):
        calls.append((args, kwargs))
        return real_with_for_update(self, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", recording_with_for_update)

    crud_product.bulk_upsert_products(
        db_session,
        user.id,
        [{"name": "Kettle", "max_price": 1.0, "min_price": 1.0, "sku": "K-1"}],
        dry_run=True,
    )

    assert calls == []


def test_bulk_upsert_insert_strips_protected_and_unknown_keys(db_session):
    from app.db.models import Product

    user = User(username="255700000713")
    db_session.add(user)
    db_session.commit()

    other_id = uuid.uuid4()
    crud_product.bulk_upsert_products(
        db_session,
        user.id,
        [{
            "name": "Shirt", "max_price": 10.0, "min_price": 5.0,
            "id": str(other_id), "slug": "hacked",
            "merchant_id": str(uuid.uuid4()), "created_at": "2020-01-01",
            "is_draft": True, "bogus_column": "x",
        }],
    )

    row = db_session.query(Product).one()
    assert row.id != other_id
    assert row.slug != "hacked"
    assert row.merchant_id == user.id
    assert row.is_draft is False


def test_crud_message_create_message_stores_attachments(db_session):
    user = User(username="255700000703")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    account = WhatsAppAccount(
        name="Shop", phone_number_id="MISC_PH3", access_token="tok", user_id=user.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    stored = crud_message.create_message(
        db_session,
        {
            "whatsapp_message_id": "wamid.att1",
            "from_phone": "255711111111",
            "type": "image",
            "body": "",
            "direction": "inbound",
            "attachments": [{"type": "image", "media_id": "MEDIA-1"}],
        },
        account.id,
    )

    assert stored.attachments == [{"type": "image", "media_id": "MEDIA-1"}]
    assert stored.body == ""
