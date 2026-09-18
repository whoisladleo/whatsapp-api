import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import get_db
from app.db.models import User
from app.api.deps import get_current_user
from app.crud import knowledge as crud_knowledge
from app.schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseInDB,
    KnowledgeBaseUpdate,
)

router = APIRouter()


@router.post("", response_model=KnowledgeBaseInDB)
async def create_knowledge(
    name: str = Form(...),
    slug: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Check if slug already exists for this user
    existing = crud_knowledge.get_knowledge_base_by_slug(db, slug, current_user.id)
    if existing:
        raise HTTPException(status_code=400, detail="Knowledge base slug already exists")

    content = await file.read()
    try:
        text_content = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Only text or markdown files are supported")

    kb_in = KnowledgeBaseCreate(name=name, slug=slug, content=text_content)
    return crud_knowledge.create_knowledge_base(db, kb_in, current_user.id)


@router.get("", response_model=List[KnowledgeBaseInDB])
def list_knowledge(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
):
    return crud_knowledge.get_multi_by_user(db, current_user.id, skip=skip, limit=limit)


@router.get("/{kb_id}", response_model=KnowledgeBaseInDB)
def get_knowledge(
    kb_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = crud_knowledge.get_knowledge_base(db, kb_id, current_user.id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


@router.put("/{kb_id}", response_model=KnowledgeBaseInDB)
async def update_knowledge(
    kb_id: uuid.UUID,
    name: Optional[str] = Form(None),
    slug: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_kb = crud_knowledge.get_knowledge_base(db, kb_id, current_user.id)
    if not db_kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    update_data = {}
    if name:
        update_data["name"] = name
    if slug:
        update_data["slug"] = slug
    if file:
        content = await file.read()
        try:
            update_data["content"] = content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Only text or markdown files are supported")

    kb_in = KnowledgeBaseUpdate(**update_data)
    return crud_knowledge.update_knowledge_base(db, db_kb, kb_in)


@router.delete("/{kb_id}")
def delete_knowledge(
    kb_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_kb = crud_knowledge.get_knowledge_base(db, kb_id, current_user.id)
    if not db_kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    crud_knowledge.delete_knowledge_base(db, db_kb)
    return {"status": "deleted"}
