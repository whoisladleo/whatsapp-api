from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class OTPRequest(BaseModel):
    username: str  # The phone number

class OTPVerify(BaseModel):
    username: str
    otp_code: str

class UserBase(BaseModel):
    username: str

class UserInDB(UserBase):
    id: int

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenPayload(BaseModel):
    sub: Optional[int] = None

class FacebookExchangeRequest(BaseModel):
    code: str

class UserProfile(BaseModel):
    id: int
    username: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    # Self-reported, only meaningful for a Facebook-login user (no phone
    # otherwise) — see app/services/subscription_access.py, which matches
    # this against the Customer who paid for the platform subscription.
    phone_number: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

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
    order_id: int
    message: str
