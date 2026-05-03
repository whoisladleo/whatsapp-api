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
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
