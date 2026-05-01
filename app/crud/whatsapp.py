from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount
from app.schemas.whatsapp import WhatsAppAccountCreate, WhatsAppAccountUpdate


def create_whatsapp_account(
    db: Session, account_in: WhatsAppAccountCreate, user_id: int
) -> WhatsAppAccount:
    # Check if this phone_number_id is already registered
    existing_account = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.phone_number_id == account_in.phone_number_id)
        .first()
    )
    if existing_account:
        return None

    db_account = WhatsAppAccount(**account_in.model_dump(), user_id=user_id)
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account


def get_account_by_phone_id(
    db: Session, phone_number_id: str, user_id: int
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
    return db_account


def get_multi_by_user(
    db: Session, user_id: int, skip: int = 0, limit: int = 100
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
    db.delete(db_account)
    db.commit()
    return db_account
