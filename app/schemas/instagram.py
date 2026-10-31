from app.schemas.common import UtcDateTime
import uuid
from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime


class InstagramAccountRead(BaseModel):
    id: uuid.UUID
    ig_business_id: str
    username: Optional[str] = None
    ai_enabled: bool
    created_at: UtcDateTime

    class Config:
        from_attributes = True


class InstagramMessageRead(BaseModel):
    id: uuid.UUID
    ig_message_id: Optional[str]
    direction: str
    body: Optional[str]
    attachments: Optional[Any] = None
    created_at: UtcDateTime

    class Config:
        from_attributes = True


class InstagramMessageSend(BaseModel):
    customer_id: uuid.UUID
    message: str


class InstagramConversationSummary(BaseModel):
    customer_id: uuid.UUID
    username: Optional[str] = None
    last_activity_at: Optional[UtcDateTime] = None
    last_message_body: Optional[str] = None
    last_message_direction: Optional[str] = None
    state: Optional[str] = None


class InstagramPendingHandoffRead(BaseModel):
    customer_id: uuid.UUID
    username: Optional[str] = None
    handoff_at: Optional[UtcDateTime] = None
    last_message_body: Optional[str] = None
