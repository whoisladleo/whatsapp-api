from app.schemas.common import UtcDateTime
import uuid
from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime


class KnowledgeBaseBase(BaseModel):
    name: str
    slug: str
    content: str


class KnowledgeBaseCreate(KnowledgeBaseBase):
    pass


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    content: Optional[str] = None


class KnowledgeBaseInDB(KnowledgeBaseBase):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: UtcDateTime
    updated_at: UtcDateTime

    model_config = ConfigDict(from_attributes=True)
