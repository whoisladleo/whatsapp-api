"""Support impersonation (#75): operator allow-list, token minting, audit.

Operators are verified identities listed in server env — an OTP-verified
phone (User.username) or a Meta-verified linked Instagram handle. The
critical invariant: NOTHING self-reported grants this. User.phone_number
is typed in by the merchant (PATCH /me), so matching it would let any
IG-login user claim an operator's number; only username (set exclusively
by the OTP verify/promote path) and InstagramAccount rows (created
exclusively by Meta OAuth callbacks) count.
"""

import logging
from datetime import timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import SupportAccessEvent, SupportAccessKind, User

logger = logging.getLogger(__name__)

IMPERSONATOR_CLAIM = "impersonator_id"


def _operator_phones() -> set:
    raw = settings.SUPPORT_OPERATOR_PHONES or ""
    return {
        normalize_phone(part)
        for part in raw.split(",")
        if normalize_phone(part.strip())
    }


def _operator_instagrams() -> set:
    raw = settings.SUPPORT_OPERATOR_INSTAGRAMS or ""
    return {
        part.strip().lstrip("@").lower()
        for part in raw.split(",")
        if part.strip().lstrip("@")
    }


def _normalize_ig_handle(handle: Optional[str]) -> Optional[str]:
    if not handle:
        return None
    cleaned = handle.strip().lstrip("@").lower()
    return cleaned or None


def operator_match(db: Session, user: User) -> Optional[str]:
    """
    Returns the human-readable operator identity (verified phone or
    @handle) when this user is allow-listed, else None. The single
    choke point for "is this user an operator" — every grant path
    (mint, session flag, per-request revalidation) funnels through here.
    """
    if user.username and normalize_phone(user.username) in _operator_phones():
        return normalize_phone(user.username)
    allowed = _operator_instagrams()
    if allowed:
        for account in user.instagram_accounts:
            handle = _normalize_ig_handle(account.username)
            if handle and handle in allowed:
                return f"@{handle}"
    return None


def is_support_operator(db: Session, user: User) -> bool:
    return operator_match(db, user) is not None


def target_label(user: User) -> str:
    """Best-effort human label for audit rows (never used for auth)."""
    if user.username:
        return user.username
    if user.phone_number:
        return user.phone_number
    for account in user.instagram_accounts:
        if account.username:
            return f"@{account.username.lstrip('@')}"
    return f"user {str(user.id)[:8]}"


def create_impersonation_token(target_id: UUID, operator_id: UUID) -> str:
    """
    Short-lived JWT whose sub is the TARGET: every existing merchant-scoped
    query keeps working unchanged, which is exactly why the impersonator
    claim, audit ledger, and sensitive-action blocks exist alongside it.
    """
    return security.create_access_token(
        target_id,
        expires_delta=timedelta(minutes=settings.IMPERSONATION_TOKEN_EXPIRE_MINUTES),
        extra_claims={IMPERSONATOR_CLAIM: str(operator_id)},
    )


def record_support_event(
    db: Session,
    operator_id,
    target_id,
    kind: SupportAccessKind,
    operator_label: Optional[str] = None,
    target_label: Optional[str] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """
    Never raises — mirrors usage.record_event: the audit write must not
    break the request it records. A missing row is logged loudly instead,
    since a gap here is a compliance hole.
    """
    try:
        db.add(SupportAccessEvent(
            operator_id=operator_id,
            operator_label=operator_label,
            target_id=target_id,
            target_label=target_label,
            kind=kind,
            method=method,
            path=path,
        detail=detail,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            f"Failed to record {kind.value} support event "
            f"operator={operator_id} target={target_id}"
        )
