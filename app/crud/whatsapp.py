import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import PhoneNumberPool, PhoneNumberStatus, WhatsAppAccount
from app.schemas.whatsapp import WhatsAppAccountCreate, WhatsAppAccountUpdate


class PartialWhatsAppCredentialsError(Exception):
    """Raised when phone_number_id/access_token aren't both supplied."""


class PhonePoolExhaustedError(Exception):
    """Raised when claiming a pool number finds no AVAILABLE rows. Used by
    the paid number-purchase flow (app/tasks/number_billing.py), which
    provisions a fresh pool row before claiming it — this should only fire
    if that provisioning step itself failed to leave an AVAILABLE row."""


class PoolAccessTokenNotConfiguredError(Exception):
    """Raised when a pool claim is attempted but WHATSAPP_POOL_ACCESS_TOKEN isn't set."""


def claim_available_pool_number(db: Session) -> Optional[PhoneNumberPool]:
    """
    Atomically claims one AVAILABLE pool row via a conditional UPDATE rather
    than SELECT ... FOR UPDATE — SQLite (used in this test suite) doesn't
    support row-level locking the way Postgres (used in production) does,
    but a guarded "UPDATE ... WHERE status = AVAILABLE" is atomic and
    correct on both, since the affected-row count tells us whether we won
    the race.
    """
    candidate = (
        db.query(PhoneNumberPool)
        .filter(PhoneNumberPool.status == PhoneNumberStatus.AVAILABLE)
        .order_by(PhoneNumberPool.id)
        .first()
    )
    if not candidate:
        return None

    claimed_count = (
        db.query(PhoneNumberPool)
        .filter(
            PhoneNumberPool.id == candidate.id,
            PhoneNumberPool.status == PhoneNumberStatus.AVAILABLE,
        )
        .update({"status": PhoneNumberStatus.ASSIGNED, "assigned_at": datetime.utcnow()})
    )
    db.commit()

    if claimed_count == 0:
        # Someone else claimed this row between our SELECT and UPDATE — the
        # row is no longer AVAILABLE, so the next call naturally skips it.
        return claim_available_pool_number(db)

    db.refresh(candidate)
    return candidate


def get_pool_number_for_account(db: Session, account_id: uuid.UUID) -> Optional[PhoneNumberPool]:
    return (
        db.query(PhoneNumberPool)
        .filter(PhoneNumberPool.whatsapp_account_id == account_id)
        .first()
    )


def release_pool_number_for_account(db: Session, account_id: uuid.UUID) -> None:
    pool_number = (
        db.query(PhoneNumberPool)
        .filter(PhoneNumberPool.whatsapp_account_id == account_id)
        .first()
    )
    if pool_number:
        pool_number.status = PhoneNumberStatus.RELEASED
        pool_number.whatsapp_account_id = None
        pool_number.released_at = datetime.utcnow()
        db.commit()


def reassign_account_to_pool_number(db: Session, db_account: WhatsAppAccount) -> WhatsAppAccount:
    """
    Switches an existing account onto a freshly claimed pool number,
    replacing its current phone_number_id/access_token. No longer reachable
    from the free self-serve API (see create_whatsapp_account) — the only
    caller now is app/tasks/number_billing.py's paid purchase flow, which
    provisions a brand-new pool row (pool_provisioning.provision_pool_number)
    immediately before calling this to claim it for the paying account.
    """
    if not settings.WHATSAPP_POOL_ACCESS_TOKEN:
        raise PoolAccessTokenNotConfiguredError()

    release_pool_number_for_account(db, db_account.id)

    pool_number = claim_available_pool_number(db)
    if not pool_number:
        raise PhonePoolExhaustedError()

    db_account.phone_number_id = pool_number.phone_number_id
    db_account.access_token = settings.WHATSAPP_POOL_ACCESS_TOKEN
    db.commit()

    pool_number.whatsapp_account_id = db_account.id
    db.commit()
    db.refresh(db_account)
    return db_account


def create_whatsapp_account(
    db: Session, account_in: WhatsAppAccountCreate, user_id: uuid.UUID
) -> Optional[WhatsAppAccount]:
    phone_number_id = account_in.phone_number_id
    access_token = account_in.access_token

    # Both are required — numbers are connected manually for now (see
    # claim_available_pool_number/the /pool endpoints for the ops-driven
    # tooling this used to trigger automatically from here).
    if not phone_number_id or not access_token:
        raise PartialWhatsAppCredentialsError()

    # Check if this phone_number_id is already registered
    existing_account = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.phone_number_id == phone_number_id)
        .first()
    )
    if existing_account:
        return None

    account_data = account_in.model_dump()
    account_data["phone_number_id"] = phone_number_id
    account_data["access_token"] = access_token

    db_account = WhatsAppAccount(**account_data, user_id=user_id)
    db.add(db_account)
    db.commit()
    db.refresh(db_account)

    from app.services.subscription_access import sync_ai_access_for_merchant

    sync_ai_access_for_merchant(db, db_account.owner)

    return db_account


def get_account_by_phone_id(
    db: Session, phone_number_id: str, user_id: uuid.UUID
) -> WhatsAppAccount:
    return (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.phone_number_id == phone_number_id,
            WhatsAppAccount.user_id == user_id,
        )
        .first()
    )


def update_whatsapp_account(
    db: Session, db_account: WhatsAppAccount, account_in: WhatsAppAccountUpdate
) -> WhatsAppAccount:
    update_data = account_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_account, field, value)
    db.commit()
    db.refresh(db_account)

    # Overrides whatever ai_enabled the request just set — a merchant
    # without a valid Taskflow subscription can't turn Amara back on for
    # themselves this way. No-op while gating isn't configured (or the
    # merchant isn't gated at all, e.g. this dashboard's own dev/test use).
    from app.services.subscription_access import sync_ai_access_for_merchant

    sync_ai_access_for_merchant(db, db_account.owner)

    return db_account


def get_multi_by_user(
    db: Session, user_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[WhatsAppAccount]:
    return (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.user_id == user_id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def delete_whatsapp_account(
    db: Session, db_account: WhatsAppAccount
) -> WhatsAppAccount:
    release_pool_number_for_account(db, db_account.id)
    db.delete(db_account)
    db.commit()
    return db_account
