import uuid
from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount
from app.schemas.account import AccountCreate, AccountUpdate
from app.schemas.whatsapp import WhatsAppAccountCreate, WhatsAppAccountUpdate
from app.crud import whatsapp as crud_whatsapp
from app.crud.whatsapp import (
    AccountHasHistoryError,
    PartialWhatsAppCredentialsError,
    PhonePoolExhaustedError,
    PoolAccessTokenNotConfiguredError,
)
from typing import List, Optional

# This module is a thin adapter over app/crud/whatsapp.py — the single
# source of truth for WhatsAppAccount creation/update/deletion (pool
# assignment, duplicate-phone_number_id handling, pool release on delete).
# It exists only to bridge AccountCreate/AccountUpdate (the schema shape
# app/api/v1/accounts.py — and the dashboard frontend — already use) onto
# WhatsAppAccountCreate/WhatsAppAccountUpdate, rather than maintaining a
# second independent implementation of the same logic.

__all__ = [
    "AccountHasHistoryError",
    "PartialWhatsAppCredentialsError",
    "PhonePoolExhaustedError",
    "PoolAccessTokenNotConfiguredError",
    "get_account_by_id",
    "get_accounts_by_user",
    "create_account",
    "update_account",
    "delete_account",
    "get_pool_two_step_pin",
    "reassign_account_to_pool_number",
]


def get_account_by_id(db: Session, account_id: uuid.UUID, user_id: uuid.UUID) -> Optional[WhatsAppAccount]:
    return db.query(WhatsAppAccount).filter(
        WhatsAppAccount.id == account_id,
        WhatsAppAccount.user_id == user_id
    ).first()


def get_accounts_by_user(db: Session, user_id: uuid.UUID) -> List[WhatsAppAccount]:
    return crud_whatsapp.get_multi_by_user(db, user_id)


def create_account(db: Session, account_in: AccountCreate, user_id: uuid.UUID) -> Optional[WhatsAppAccount]:
    whatsapp_account_in = WhatsAppAccountCreate(**account_in.model_dump())
    return crud_whatsapp.create_whatsapp_account(db, whatsapp_account_in, user_id)


def update_account(db: Session, db_account: WhatsAppAccount, account_in: AccountUpdate) -> WhatsAppAccount:
    whatsapp_account_in = WhatsAppAccountUpdate(**account_in.model_dump(exclude_unset=True))
    return crud_whatsapp.update_whatsapp_account(db, db_account, whatsapp_account_in)


def delete_account(db: Session, db_account: WhatsAppAccount) -> WhatsAppAccount:
    return crud_whatsapp.delete_whatsapp_account(db, db_account)


def get_pool_two_step_pin(db: Session, account_id: uuid.UUID) -> Optional[str]:
    pool_number = crud_whatsapp.get_pool_number_for_account(db, account_id)
    return pool_number.two_step_pin if pool_number else None


def reassign_account_to_pool_number(db: Session, db_account: WhatsAppAccount) -> WhatsAppAccount:
    return crud_whatsapp.reassign_account_to_pool_number(db, db_account)
