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
    instagram_account_id: Optional[int] = None
    channel: str = "whatsapp"
    product_name: Optional[str] = None
    customer_phone: Optional[str] = None
    reserved_until: Optional[datetime] = None
    created_at: datetime


class OrderDetail(OrderRead):
    delivery_address: Optional[str] = None
    tunzaa_transaction_id: Optional[str] = None
    installments: List[InstallmentRead] = []
    # Only populated for INSTALLMENT orders, synced from Tunzaa's own
    # plan-level ledger on each payment webhook — see
    # app/api/v1/tunzaa_webhook.py's _reconcile_installment_plan.
    paid_amount: Optional[float] = None
    remaining_balance: Optional[float] = None
    percentage_complete: Optional[float] = None
