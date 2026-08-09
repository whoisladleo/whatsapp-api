from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount
from app.schemas.account import AccountCreate, AccountUpdate
from typing import List, Optional

def get_account_by_id(db: Session, account_id: int, user_id: int) -> Optional[WhatsAppAccount]:
    return db.query(WhatsAppAccount).filter(
        WhatsAppAccount.id == account_id,
        WhatsAppAccount.user_id == user_id
    ).first()

def get_accounts_by_user(db: Session, user_id: int) -> List[WhatsAppAccount]:
    return db.query(WhatsAppAccount).filter(WhatsAppAccount.user_id == user_id).all()

def create_account(db: Session, account_in: AccountCreate, user_id: int) -> WhatsAppAccount:
    db_account = WhatsAppAccount(
        **account_in.model_dump(),
        user_id=user_id
    )
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account

def update_account(db: Session, db_account: WhatsAppAccount, account_in: AccountUpdate) -> WhatsAppAccount:
    update_data = account_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_account, field, value)
    
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account

def delete_account(db: Session, db_account: WhatsAppAccount) -> None:
    db.delete(db_account)
    db.commit()
