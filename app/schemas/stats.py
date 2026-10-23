from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class ReplyUsage(BaseModel):
    used: int
    limit: int
    percent_used: float


class ProductStats(BaseModel):
    product: str
    quotes: int
    orders: int
    paid_orders: int


class MerchantStats(BaseModel):
    period_start: datetime
    period_end: datetime
    replies: ReplyUsage
    buyers: int
    handoffs: int
    orders_created: int
    orders_paid: int
    tzs_collected: float
    # Paid orders ÷ buyers who messaged, both in this period. None with no buyers.
    conversion_rate: Optional[float]
    top_products: List[ProductStats]
