import uuid
from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import jwt
from app.db.session import get_db
from app.db.models import User
from app.core.config import settings
from app.services.support_access import IMPERSONATOR_CLAIM, is_support_operator

security_scheme = HTTPBearer()


def decode_access_token(credentials: str) -> dict:
    try:
        return jwt.decode(
            credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validated_impersonator_id(db: Session, payload: dict) -> Optional[uuid.UUID]:
    """
    None for a normal token. For an impersonated token, returns the
    operator id after validating the operator row still exists AND is
    still allow-listed — removing someone from the env list kills their
    live sessions on next use (modulo the 30-minute token expiry).
    """
    raw = payload.get(IMPERSONATOR_CLAIM)
    if raw is None:
        return None
    try:
        operator_id = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    operator = db.query(User).filter(User.id == operator_id).first()
    if not operator or not is_support_operator(db, operator):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator access revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return operator_id


async def get_current_user(
    db: Session = Depends(get_db),
    token: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> User:
    # NOTE: for an impersonated token this is the TARGET merchant, not the
    # operator — every merchant-scoped query below keeps working unchanged
    # by design. Endpoints that must tell them apart depend on
    # get_impersonation / forbid_impersonated instead.
    payload = decode_access_token(token.credentials)
    _validated_impersonator_id(db, payload)
    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@dataclass
class Impersonation:
    operator_id: uuid.UUID


async def get_impersonation(
    db: Session = Depends(get_db),
    token: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> Optional[Impersonation]:
    """
    None for a normal user token. For an impersonated token, validates the
    operator row still exists AND is still allow-listed — removing someone
    from the env list kills their live sessions on next use (modulo the
    30-minute token expiry) — else 401.
    """
    payload = decode_access_token(token.credentials)
    operator_id = _validated_impersonator_id(db, payload)
    if operator_id is None:
        return None
    return Impersonation(operator_id=operator_id)


async def forbid_impersonated(
    impersonation: Optional[Impersonation] = Depends(get_impersonation),
) -> None:
    """
    Guard for endpoints an impersonated session must never reach: minting
    another impersonation (no nesting), and anything that moves money.
    """
    if impersonation is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not available while impersonating a merchant",
        )
