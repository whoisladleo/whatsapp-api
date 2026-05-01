from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from datetime import timedelta
from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.schemas.auth import Token, UserCreate, UserInDB, UserLogin
from app.crud import user as crud_user

router = APIRouter()


@router.post("/login", response_model=Token)
def login_access_token(
    db: Session = Depends(get_db), login_data: UserLogin = Body(...)
):
    user = crud_user.get_user_by_username(db, login_data.username)
    if not user or not security.verify_password(
        login_data.password, user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }


@router.post("/register", response_model=UserInDB)
def register_user(user_in: UserCreate, db: Session = Depends(get_db)):
    user = crud_user.get_user_by_username(db, user_in.username)
    if user:
        raise HTTPException(
            status_code=400,
            detail="Username already registered",
        )
    return crud_user.create_user(db, user_in)
