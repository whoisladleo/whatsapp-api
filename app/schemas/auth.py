from pydantic import BaseModel, ConfigDict
from typing import Optional

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
