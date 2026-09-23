import uuid
from typing import Optional

from sqlalchemy.orm import Session
from app.db.models import User
from datetime import datetime


def get_user_by_username(db: Session, username: str) -> User:
    return db.query(User).filter(User.username == username).first()


def get_user_by_facebook_id(db: Session, facebook_id: str) -> Optional[User]:
    return db.query(User).filter(User.facebook_id == facebook_id).first()


def get_user(db: Session, user_id: uuid.UUID) -> User:
    return db.query(User).filter(User.id == user_id).first()


def create_user(db: Session, username: str) -> User:
    db_user = User(
        username=username,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def create_facebook_user(
    db: Session, facebook_id: str, name: Optional[str], email: Optional[str]
) -> User:
    db_user = User(facebook_id=facebook_id, name=name, email=email)
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


def get_user_by_phone_number(db: Session, phone_number: str) -> Optional[User]:
    return db.query(User).filter(User.phone_number == phone_number).first()


def set_user_phone_number(db: Session, user: User, phone_number: Optional[str]) -> User:
    user.phone_number = phone_number
    db.commit()
    db.refresh(user)
    return user


def promote_phone_number_to_username(db: Session, user: User, phone_number: str) -> User:
    """
    Called from /auth/request-otp when a phone number doesn't match any
    User.username but does match a User.phone_number (a Facebook-login
    merchant who's self-reported their phone via PATCH /auth/me or Pay
    Now) — promotes it to username instead of creating a second, unlinked
    User for the same person. Safe by construction: callers only reach
    this after get_user_by_username(db, phone_number) already returned
    None, so no other user currently holds this username.
    """
    user.username = phone_number
    db.commit()
    db.refresh(user)
    return user
