from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class InstagramAccountRead(BaseModel):
    id: int
    ig_business_id: str
    username: Optional[str] = None
    ai_enabled: bool
    created_at: datetime

    class Config:
        from_attributes = True
