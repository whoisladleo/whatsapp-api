import uuid
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
    # Optional at the type level only so a missing field gives the clearer
    # "both are required" error from the endpoint/crud layer rather than a
    # generic Pydantic validation error. Both are required in practice —
    # numbers are connected manually for now (see #16), no more pool
    # auto-assign when both are omitted. Mirrors WhatsAppAccountCreate.
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
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    # We omit access_token from response for security

    class Config:
        from_attributes = True


class AccountMetaStatus(BaseModel):
    """Live phone-number status as Meta itself reports it, not our own DB state."""
    id: Optional[str] = None
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


class NumberSubscriptionResponse(BaseModel):
    """Billing state for an on-demand purchased number — see
    app/services/number_billing.py and app/db/models.py's NumberSubscription."""
    id: uuid.UUID
    status: str
    monthly_amount: float
    next_billing_at: Optional[datetime] = None
    retry_count: int
    created_at: datetime

    class Config:
        from_attributes = True
