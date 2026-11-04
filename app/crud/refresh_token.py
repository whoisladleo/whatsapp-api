import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import RefreshToken


def create_refresh_token(
    db: Session, user_id: uuid.UUID, token_hash: str, expires_at: datetime
) -> RefreshToken:
    record = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_refresh_token_by_hash(db: Session, token_hash: str) -> Optional[RefreshToken]:
    return db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()


def revoke_refresh_token(db: Session, record: RefreshToken) -> RefreshToken:
    record.revoked_at = datetime.utcnow()
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def try_revoke_refresh_token(db: Session, record_id: uuid.UUID) -> bool:
    """
    Atomically revokes one unrevoked refresh row. True iff this caller won
    the race (rowcount==1); False means another request already revoked it.
    Same conditional-UPDATE pattern as claim_available_pool_number —
    correct on both SQLite tests and Postgres prod.
    """
    revoked_count = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.id == record_id,
            RefreshToken.revoked_at.is_(None),
        )
        .update({"revoked_at": datetime.utcnow()})
    )
    db.commit()
    return revoked_count == 1
