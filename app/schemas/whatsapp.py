from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime


class WhatsAppAccountBase(BaseModel):
    name: str
    phone_number_id: str
    access_token: str
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None


class WhatsAppAccountCreate(WhatsAppAccountBase):
    pass


class WhatsAppAccountUpdate(BaseModel):
    name: Optional[str] = None
    access_token: Optional[str] = None
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None


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
