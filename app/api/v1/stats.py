from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.stats import MerchantStats
from app.services.stats import merchant_stats

router = APIRouter()


@router.get("", response_model=MerchantStats)
def get_stats(
    period: Literal["current", "previous"] = "current",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The logged-in merchant's Amara usage and sales for one billing period (#41)."""
    return merchant_stats(db, current_user, period)
