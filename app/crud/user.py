from sqlalchemy.orm import Session
from app.db.models import User
from datetime import datetime


def get_user_by_username(db: Session, username: str) -> User:
    return db.query(User).filter(User.username == username).first()


def get_user(db: Session, user_id: int) -> User:
    return db.query(User).filter(User.id == user_id).first()


def create_user(db: Session, username: str) -> User:
    db_user = User(
        username=username,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def set_user_otp(db: Session, user: User, otp_code: str, expires_at: datetime) -> User:
    user.otp_code = otp_code
    user.otp_expires_at = expires_at
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def clear_user_otp(db: Session, user: User) -> User:
    user.otp_code = None
    user.otp_expires_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
