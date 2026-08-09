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
