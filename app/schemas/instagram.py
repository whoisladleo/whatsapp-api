from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class InstagramAccountRead(BaseModel):
    id: int
    ig_business_id: str
    username: Optional[str] = None
    ai_enabled: bool
    created_at: datetime

    class Config:
        from_attributes = True


class InstagramMessageRead(BaseModel):
    id: int
    ig_message_id: Optional[str]
    direction: str
    body: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class InstagramMessageSend(BaseModel):
    customer_id: int
    message: str


class InstagramConversationSummary(BaseModel):
    customer_id: int
    username: Optional[str] = None
    last_activity_at: Optional[datetime] = None
    last_message_body: Optional[str] = None
    last_message_direction: Optional[str] = None
    state: Optional[str] = None


class InstagramPendingHandoffRead(BaseModel):
    customer_id: int
    username: Optional[str] = None
    handoff_at: Optional[datetime] = None
    last_message_body: Optional[str] = None
