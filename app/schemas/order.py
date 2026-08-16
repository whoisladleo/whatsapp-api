from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from app.db.models import OrderStatus, PaymentType, InstallmentStatus


class InstallmentRead(BaseModel):
    id: int
    amount_due: float
    due_date: datetime
    status: InstallmentStatus

    class Config:
        from_attributes = True


class OrderRead(BaseModel):
    id: int
    status: OrderStatus
    payment_type: Optional[PaymentType] = None
    quantity: int
    agreed_price: Optional[float] = None
    account_id: Optional[int] = None
    product_name: Optional[str] = None
    customer_phone: Optional[str] = None
    reserved_until: Optional[datetime] = None
    created_at: datetime


class OrderDetail(OrderRead):
    delivery_address: Optional[str] = None
    tunzaa_transaction_id: Optional[str] = None
    installments: List[InstallmentRead] = []
