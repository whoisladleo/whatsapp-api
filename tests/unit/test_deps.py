import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import get_current_user
from app.core.security import create_access_token
from app.db.models import User


async def test_get_current_user_success(db_session):
    user = User(username="255700000400")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(user.id)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    result = await get_current_user(db=db_session, token=creds)

    assert result.id == user.id


async def test_get_current_user_invalid_token_raises_401(db_session):
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(db=db_session, token=creds)

    assert exc_info.value.status_code == 401


async def test_get_current_user_missing_sub_claim_raises_401(db_session, monkeypatch):
    import jwt as pyjwt

    from app.core.config import settings

    token = pyjwt.encode({"not_sub": "x"}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(db=db_session, token=creds)

    assert exc_info.value.status_code == 401


async def test_get_current_user_unknown_user_id_raises_404(db_session):
    token = create_access_token(uuid.uuid4())
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(db=db_session, token=creds)

    assert exc_info.value.status_code == 404
