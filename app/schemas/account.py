from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class AccountBase(BaseModel):
    name: str
    phone_number_id: str
    verify_token: Optional[str] = None
    callback_url: Optional[str] = None
    product_slug: Optional[str] = None
    ai_enabled: bool = False
    system_prompt: Optional[str] = None

class AccountCreate(AccountBase):
    access_token: str

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
