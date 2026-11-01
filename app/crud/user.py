import uuid
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload
from app.core.security import normalize_phone, sha256_hex, verify_sha256_hex
from app.db.models import User
from datetime import datetime


def resolve_handoff_number(user: User) -> Optional[str]:
    """Merchant-alert recipient (#105, #106): the explicit handoff number
    wins; else the OTP `username`; else the profile `phone_number`. Values
    that aren't dialable (social-login usernames, blanks) are skipped, so
    an unset chain resolves to None and the alert stays silent. Used by
    handoff alerts and usage warnings alike — one resolution, not two."""
    for raw in (user.handoff_phone_number, user.username, user.phone_number):
        normalized = normalize_phone(raw) if raw else ""
        if normalized and len(normalized) >= 9:
            return normalized
    return None


def get_user_by_username(db: Session, username: str) -> User:
    return db.query(User).filter(User.username == username).first()


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


def create_instagram_user(db: Session, name: Optional[str]) -> User:
    """
    A merchant who signed up via Instagram Login — no phone yet (username
    stays NULL until they self-report one via PATCH /auth/me or verify via
    OTP, which promotes it; see promote_phone_number_to_username). Identity
    is anchored on the InstagramAccount row upserted alongside, not here.
    """
    db_user = User(name=name)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def set_user_otp(db: Session, user: User, otp_code: str, expires_at: datetime) -> User:
    # The DB holds the sha256 of the code, never the code itself — a read
    # of this row (backup, log, admin query) must not yield a live login.
    user.otp_code = sha256_hex(otp_code)
    user.otp_expires_at = expires_at
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def verify_user_otp(user: User, otp_code: str) -> bool:
    """Constant-time check of a submitted code against the stored hash.
    False when no code is set — callers treat that like a wrong guess."""
    return verify_sha256_hex(user.otp_code, otp_code)


def clear_user_otp(db: Session, user: User) -> User:
    user.otp_code = None
    user.otp_expires_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_phone_number(db: Session, phone_number: str) -> Optional[User]:
    return db.query(User).filter(User.phone_number == phone_number).first()


def search_users(db: Session, q: Optional[str], cap: int = 1000) -> List[User]:
    """
    Support-list candidates for GET /auth/merchants: newest first, optional
    case-insensitive match on username / phone_number / business_name / name.
    Eager-loads instagram_accounts so the caller's operator check doesn't
    N+1. Capped (not paged): the caller filters operators out in Python —
    operator status lives in env + linked rows, not a column — then pages.
    Fine at support scale; revisit past ~1k merchants.
    """
    query = db.query(User).options(selectinload(User.instagram_accounts))
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(or_(
            User.username.ilike(like),
            User.phone_number.ilike(like),
            User.business_name.ilike(like),
            User.name.ilike(like),
        ))
    return query.order_by(User.created_at.desc()).limit(cap).all()


def set_user_phone_number(db: Session, user: User, phone_number: Optional[str]) -> User:
    user.phone_number = phone_number
    db.commit()
    db.refresh(user)
    return user


def set_user_handoff_number(db: Session, user: User, phone_number: Optional[str]) -> User:
    user.handoff_phone_number = phone_number
    db.commit()
    db.refresh(user)
    return user


def promote_phone_number_to_username(db: Session, user: User, phone_number: str) -> User:
    """
    Called from /auth/request-otp when a phone number doesn't match any
    User.username but does match a User.phone_number (an Instagram-login
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
