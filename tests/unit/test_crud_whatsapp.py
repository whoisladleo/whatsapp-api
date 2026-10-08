import pytest

from app.crud import whatsapp as crud_whatsapp
from app.db.models import PhoneNumberPool, PhoneNumberStatus, User, WhatsAppAccount
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


def _pool_number(db_session, phone_number_id="POOL1", display_name="Pool #1"):
    entry = PhoneNumberPool(phone_number_id=phone_number_id, display_name=display_name)
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def test_claim_available_pool_number_marks_it_assigned(db_session):
    entry = _pool_number(db_session)

    claimed = crud_whatsapp.claim_available_pool_number(db_session)

    assert claimed.id == entry.id
    assert claimed.status == PhoneNumberStatus.ASSIGNED
    assert claimed.assigned_at is not None


def test_claim_available_pool_number_returns_none_when_empty(db_session):
    assert crud_whatsapp.claim_available_pool_number(db_session) is None


def test_claim_available_pool_number_second_claim_never_reclaims_same_row(db_session):
    """
    Not true concurrency (single-threaded SQLite test session), but proves
    the atomicity property that matters: once a row is claimed, a second
    caller can never walk away with the same already-assigned row.
    """
    _pool_number(db_session)

    first = crud_whatsapp.claim_available_pool_number(db_session)
    second = crud_whatsapp.claim_available_pool_number(db_session)

    assert first is not None
    assert second is None


def test_claim_available_pool_number_retries_after_lost_race(db_session, monkeypatch):
    """
    Simulates the "lost the race between SELECT and UPDATE" branch (another
    process/request claimed the same row first) by making the first
    .update() call report zero rows affected, then letting it proceed
    normally — proves the function retries and still succeeds.
    """
    from sqlalchemy.orm import Query

    _pool_number(db_session, "POOL_RACE")
    original_update = Query.update
    call_count = {"n": 0}

    def flaky_update(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return 0
        return original_update(self, *args, **kwargs)

    monkeypatch.setattr(Query, "update", flaky_update)

    claimed = crud_whatsapp.claim_available_pool_number(db_session)

    assert claimed is not None
    assert claimed.phone_number_id == "POOL_RACE"
    assert call_count["n"] == 2


def test_claim_available_pool_number_skips_assigned_and_released_rows(db_session):
    _pool_number(db_session, "POOL_ASSIGNED")
    already_assigned = crud_whatsapp.claim_available_pool_number(db_session)
    already_assigned.status = PhoneNumberStatus.RELEASED
    db_session.commit()

    fresh = _pool_number(db_session, "POOL_FRESH")

    claimed = crud_whatsapp.claim_available_pool_number(db_session)

    assert claimed.id == fresh.id


def test_create_whatsapp_account_partial_credentials_raises(db_session):
    user = _user(db_session, "255700000309")

    with pytest.raises(crud_whatsapp.PartialWhatsAppCredentialsError):
        crud_whatsapp.create_whatsapp_account(
            db_session,
            WhatsAppAccountCreate(name="Shop", phone_number_id="ONLY_ONE"),
            user.id,
        )


def test_create_whatsapp_account_both_omitted_raises(db_session):
    # Numbers are connected manually for now (see #16) — no more silent
    # pool auto-assign when both fields are omitted.
    user = _user(db_session, "255700000309b")

    with pytest.raises(crud_whatsapp.PartialWhatsAppCredentialsError):
        crud_whatsapp.create_whatsapp_account(
            db_session, WhatsAppAccountCreate(name="Shop"), user.id
        )


def test_create_whatsapp_account_byon_path_ignores_pool(db_session):
    user = _user(db_session, "255700000310")
    _pool_number(db_session, "POOL_UNTOUCHED")

    account = crud_whatsapp.create_whatsapp_account(
        db_session,
        WhatsAppAccountCreate(name="Shop", phone_number_id="OWN1", access_token="own-tok"),
        user.id,
    )

    assert account.phone_number_id == "OWN1"
    pool_row = (
        db_session.query(PhoneNumberPool)
        .filter(PhoneNumberPool.phone_number_id == "POOL_UNTOUCHED")
        .first()
    )
    assert pool_row.status == PhoneNumberStatus.AVAILABLE


def test_delete_whatsapp_account_releases_pool_number(db_session):
    # An account can still end up linked to a PhoneNumberPool row via the
    # paid purchase flow or ops-driven manual assignment (see #16) — not
    # exercised here, just the resulting linked state delete must release.
    user = _user(db_session, "255700000312")
    account = crud_whatsapp.create_whatsapp_account(
        db_session,
        WhatsAppAccountCreate(name="Shop", phone_number_id="OWN_RELEASE", access_token="own-tok"),
        user.id,
    )
    pool_entry = _pool_number(db_session, "POOL_RELEASE")
    pool_entry.status = PhoneNumberStatus.ASSIGNED
    pool_entry.whatsapp_account_id = account.id
    db_session.commit()

    crud_whatsapp.delete_whatsapp_account(db_session, account)

    pool_row = (
        db_session.query(PhoneNumberPool)
        .filter(PhoneNumberPool.phone_number_id == "POOL_RELEASE")
        .first()
    )
    assert pool_row.status == PhoneNumberStatus.RELEASED
    assert pool_row.whatsapp_account_id is None
    assert pool_row.released_at is not None


def test_delete_whatsapp_account_without_pool_number_is_a_noop_for_pool(db_session):
    user = _user(db_session, "255700000313")
    account = crud_whatsapp.create_whatsapp_account(
        db_session,
        WhatsAppAccountCreate(name="Shop", phone_number_id="OWN2", access_token="own-tok"),
        user.id,
    )

    # Should not raise even though there's no associated pool row.
    crud_whatsapp.delete_whatsapp_account(db_session, account)
