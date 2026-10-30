import uuid
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime

class OTPRequest(BaseModel):
    username: str  # The phone number

class OTPVerify(BaseModel):
    username: str
    otp_code: str

class UserBase(BaseModel):
    username: str

class UserInDB(UserBase):
    id: uuid.UUID

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenPayload(BaseModel):
    sub: Optional[str] = None

class InstagramExchangeRequest(BaseModel):
    code: str

class ImpersonateRequest(BaseModel):
    # Merchant phone as support knows it (any format — normalized before
    # matching username first, then self-reported phone_number).
    target_phone: str

class ImpersonatorRef(BaseModel):
    id: uuid.UUID
    label: Optional[str] = None

class UserProfile(BaseModel):
    id: uuid.UUID
    username: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    # Self-reported, only meaningful when the account has no phone tied to
    # it (no username) — see app/services/subscription_access.py, which
    # matches this against the Customer who paid for the platform subscription.
    phone_number: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class SessionRead(BaseModel):
    # Who this token acts as (the TARGET while impersonating), whether the
    # acting user may mint impersonations, and who is behind the mask.
    user: UserProfile
    is_operator: bool = False
    impersonated_by: Optional[ImpersonatorRef] = None

class MerchantListItem(BaseModel):
    # One row of the operator merchant list: identity + display fields only,
    # never secrets (no OTP state, no tokens). from_attributes lets the
    # endpoint return ORM rows directly.
    id: uuid.UUID
    username: Optional[str] = None
    phone_number: Optional[str] = None
    business_name: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class MerchantListRead(BaseModel):
    # total counts ALL impersonable merchants (post search + post operator
    # exclusion), not just this page — the /admin UI pages against it.
    items: List[MerchantListItem]
    total: int

class UserProfileUpdate(BaseModel):
    phone_number: Optional[str] = None

class MySubscriptionRead(BaseModel):
    # None status = no ProductSubscription found at all for this user —
    # distinct from having one that's CANCELLED/PAYMENT_FAILED, though both
    # deny AI access identically (see subscription_access.is_merchant_allowed_ai).
    status: Optional[str] = None
    monthly_amount: Optional[float] = None
    next_billing_at: Optional[datetime] = None
    retry_count: Optional[int] = None
    past_due_since: Optional[datetime] = None

class PayNowRequest(BaseModel):
    # Only needed if no phone is already on file (User.username/phone_number)
    # — entered inline in the dashboard's Pay Now flow rather than requiring
    # a separate trip to set it first.
    phone_number: Optional[str] = None

class PayNowResponse(BaseModel):
    order_id: uuid.UUID
    message: str
