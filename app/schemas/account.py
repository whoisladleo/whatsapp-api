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
    # True releases the account's current number (if pool-assigned) and
    # claims a fresh one from the pool, replacing phone_number_id/
    # access_token — see reassign_account_to_pool_number. Mutually
    # exclusive with supplying phone_number_id/access_token; the endpoint
    # handles that combination before touching either.
    reassign_from_pool: bool = False

class AccountResponse(AccountBase):
    id: int
    user_id: int
    created_at: datetime
    # We omit access_token from response for security

    class Config:
        from_attributes = True


class AccountMetaStatus(BaseModel):
    """Live phone-number status as Meta itself reports it, not our own DB state."""
    verified_name: Optional[str] = None
    display_phone_number: Optional[str] = None
    quality_rating: Optional[str] = None
    code_verification_status: Optional[str] = None
    status: Optional[str] = None
    platform_type: Optional[str] = None
    # Set while a display-name change is pending Meta's review.
    new_display_name: Optional[str] = None
    new_name_status: Optional[str] = None
    # From our own DB, not Meta — see app/crud/message.py's
    # get_recent_send_failures. Meta's own status fields above can all look
    # healthy while every send is actually failing (e.g. code 131037,
    # "display name needs approval"), so this is the only reliable signal.
    recent_send_failures: int = 0
    last_send_failure_reason: Optional[str] = None
    last_send_failure_at: Optional[datetime] = None
