from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime


class WhatsAppAccountBase(BaseModel):
    name: str
    phone_number_id: str
    access_token: str
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None
    product_slug: Optional[str] = None
    ai_enabled: bool = False
    system_prompt: Optional[str] = None


class WhatsAppAccountCreate(WhatsAppAccountBase):
    # Optional here (unlike the base): omit both to auto-assign a number
    # from the pool, or supply both to bring your own Meta credentials.
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None


class WhatsAppAccountUpdate(BaseModel):
    name: Optional[str] = None
    access_token: Optional[str] = None
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None
    product_slug: Optional[str] = None
    ai_enabled: Optional[bool] = None
    system_prompt: Optional[str] = None


class WhatsAppAccountInDB(WhatsAppAccountBase):
    id: int
    user_id: int

    model_config = ConfigDict(from_attributes=True)


class MessageSend(BaseModel):
    to: str
    type: str = "template"  # template, text, image, etc
    template_name: Optional[str] = "hello_world"
    text_body: Optional[str] = None
    media_id: Optional[str] = None
    caption: Optional[str] = None


class MessageRead(BaseModel):
    id: int
    whatsapp_message_id: Optional[str]
    from_phone: Optional[str]
    to_phone: Optional[str]
    type: str
    body: Optional[str]
    direction: str
    status: str
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class PendingHandoffRead(BaseModel):
    customer_phone: str
    handoff_at: Optional[datetime] = None
    last_message_body: Optional[str] = None
