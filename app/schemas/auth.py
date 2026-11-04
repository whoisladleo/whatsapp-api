from app.schemas.common import UtcDateTime
import uuid
from pydantic import BaseModel, ConfigDict, model_validator
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
    # Present on real logins (OTP verify, IG exchange) — the dashboard keeps
    # it in an httpOnly cookie and rotates it via POST /auth/refresh. None
    # for impersonation tokens, which stay short-lived and non-refreshable.
    refresh_token: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # Optional so logout never fails for lack of one — no token just means
    # there's nothing server-side left to revoke.
    refresh_token: Optional[str] = None

class TokenPayload(BaseModel):
    sub: Optional[str] = None

class InstagramExchangeRequest(BaseModel):
    code: str

class ImpersonateRequest(BaseModel):
    # Merchant ID as listed by GET /auth/merchants — the primary key, and
    # the only one that reaches phoneless IG-signup merchants.
    target_user_id: Optional[uuid.UUID] = None
    # Merchant phone as support knows it (any format — normalized before
    # matching username first, then self-reported phone_number). Fallback
    # for callers without the ID; ignored when target_user_id is present.
    target_phone: Optional[str] = None

    @model_validator(mode="after")
    def _require_a_target(self):
        if self.target_user_id is None and not (self.target_phone or "").strip():
            raise ValueError("Provide target_user_id or target_phone.")
        return self

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
    # Explicit handoff-alert override (dashboard settings) — see
    # crud_user.resolve_handoff_number.
    handoff_phone_number: Optional[str] = None

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
    created_at: Optional[UtcDateTime] = None
    trial_ends_at: Optional[UtcDateTime] = None

    model_config = ConfigDict(from_attributes=True)

class TrialGrantRequest(BaseModel):
    # Exactly one of days / ends_at: days counts from now (grant or
    # re-grant), ends_at pins an absolute cutoff. Both set or both
    # missing is a 422 — the caller must mean one of them.
    user_id: uuid.UUID
    days: Optional[int] = None
    ends_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _exactly_one_shape(self):
        if (self.days is None) == (self.ends_at is None):
            raise ValueError("Provide exactly one of days or ends_at.")
        if self.days is not None and self.days < 1:
            raise ValueError("days must be at least 1.")
        return self

class TrialRead(BaseModel):
    user_id: uuid.UUID
    trial_ends_at: Optional[UtcDateTime] = None
    # Computed post-write, so the admin UI shows the effect immediately
    # instead of guessing whether the grant took.
    amara_active: bool

class MerchantListRead(BaseModel):
    # total counts ALL impersonable merchants (post search + post operator
    # exclusion), not just this page — the /admin UI pages against it.
    items: List[MerchantListItem]
    total: int

class UserProfileUpdate(BaseModel):
    phone_number: Optional[str] = None
    handoff_phone_number: Optional[str] = None

class MySubscriptionRead(BaseModel):
    # None status = no ProductSubscription found at all for this user —
    # distinct from having one that's CANCELLED/PAYMENT_FAILED, though both
    # deny AI access identically (see subscription_access.is_merchant_allowed_ai).
    status: Optional[str] = None
    monthly_amount: Optional[float] = None
    next_billing_at: Optional[UtcDateTime] = None
    retry_count: Optional[int] = None
    past_due_since: Optional[UtcDateTime] = None
    # The backend's own access verdict (grace periods, plan-seller
    # exemption, no-plans-configured) — the dashboard banner keys off this
    # rather than reimplementing subscription_access.is_merchant_allowed_ai.
    amara_active: bool = True
    # Whether resolve_handoff_number finds a dialable alert target — the
    # dashboard's WhatsApp-only upsell banner keys off this rather than
    # mirroring the resolver client-side (#145). Defaults True (no banner):
    # a missing value must never nag merchants who have a number.
    has_handoff_number: bool = True

class PayNowRequest(BaseModel):
    # Only needed if no phone is already on file (User.username/phone_number)
    # — entered inline in the dashboard's Pay Now flow rather than requiring
    # a separate trip to set it first.
    phone_number: Optional[str] = None
    # Which plan SKU to pay for. Optional while exactly one plan exists;
    # required once there are several to choose from (#90).
    product_id: Optional[uuid.UUID] = None

class PayNowResponse(BaseModel):
    order_id: uuid.UUID
    message: str
