from sqlalchemy.orm import Session
from app.db.models import User
from app.core import security
from app.schemas.auth import UserCreate


def get_user_by_username(db: Session, username: str) -> User:
    return db.query(User).filter(User.username == username).first()


def get_user(db: Session, user_id: int) -> User:
    return db.query(User).filter(User.id == user_id).first()


def create_user(db: Session, user_in: UserCreate) -> User:
    db_user = User(
        username=user_in.username,
        hashed_password=security.get_password_hash(user_in.password),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user
