from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class AccountBase(BaseModel):
    name: str
    phone_number_id: str
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None
    product_slug: Optional[str] = None
    ai_enabled: bool = True
    system_prompt: Optional[str] = None

class AccountCreate(AccountBase):
    # Optional here (unlike AccountBase): omit both phone_number_id and
    # access_token to auto-assign a number from the pool, or supply both to
    # bring your own Meta credentials. Mirrors WhatsAppAccountCreate.
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None

class AccountUpdate(BaseModel):
    name: Optional[str] = None
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None
    product_slug: Optional[str] = None
    ai_enabled: Optional[bool] = None
    system_prompt: Optional[str] = None

class AccountResponse(AccountBase):
    id: int
    user_id: int
    created_at: datetime
    # We omit access_token from response for security

    class Config:
        from_attributes = True
