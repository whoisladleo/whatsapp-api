import uuid
from sqlalchemy.orm import Session
from app.db.models import KnowledgeBase
from app.schemas.knowledge import KnowledgeBaseCreate, KnowledgeBaseUpdate
from typing import List, Optional


def create_knowledge_base(
    db: Session, kb_in: KnowledgeBaseCreate, user_id: uuid.UUID
) -> KnowledgeBase:
    db_kb = KnowledgeBase(**kb_in.model_dump(), user_id=user_id)
    db.add(db_kb)
    db.commit()
    db.refresh(db_kb)
    return db_kb


def get_knowledge_base(db: Session, kb_id: uuid.UUID, user_id: uuid.UUID) -> Optional[KnowledgeBase]:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == user_id)
        .first()
    )


def get_knowledge_base_by_slug(
    db: Session, slug: str, user_id: uuid.UUID
) -> Optional[KnowledgeBase]:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.slug == slug, KnowledgeBase.user_id == user_id)
        .first()
    )


def get_multi_by_user(
    db: Session, user_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> List[KnowledgeBase]:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.user_id == user_id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def update_knowledge_base(
    db: Session, db_kb: KnowledgeBase, kb_in: KnowledgeBaseUpdate
) -> KnowledgeBase:
    update_data = kb_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_kb, field, value)
    db.commit()
    db.refresh(db_kb)
    return db_kb


def delete_knowledge_base(db: Session, db_kb: KnowledgeBase) -> KnowledgeBase:
    db.delete(db_kb)
    db.commit()
    return db_kb
