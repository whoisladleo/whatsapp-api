from app.schemas.common import UtcDateTime
import uuid
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from app.db.models import OrderStatus, PaymentType, InstallmentStatus, ProductSubscriptionStatus


class InstallmentRead(BaseModel):
    id: uuid.UUID
    amount_due: float
    due_date: UtcDateTime
    status: InstallmentStatus

    class Config:
        from_attributes = True


class OrderRead(BaseModel):
    id: uuid.UUID
    status: OrderStatus
    payment_type: Optional[PaymentType] = None
    quantity: int
    agreed_price: Optional[float] = None
    account_id: Optional[uuid.UUID] = None
    instagram_account_id: Optional[uuid.UUID] = None
    channel: str = "whatsapp"
    product_name: Optional[str] = None
    customer_phone: Optional[str] = None
    reserved_until: Optional[UtcDateTime] = None
    created_at: UtcDateTime
    # Present only when Order.product.is_subscription — see
    # app/services/product_subscription.py. This is the merchant-visibility
    # surface for recurring billing: a merchant sees a subscription go
    # PAST_DUE/CANCELLED here even without a WhatsApp account to receive
    # the best-effort push alert on.
    subscription_status: Optional[ProductSubscriptionStatus] = None
    subscription_next_billing_at: Optional[UtcDateTime] = None
    subscription_retry_count: Optional[int] = None
    # Refund-owed marker — "MANUAL" when a late payment arrived with no
    # stock to fulfill it (see Order.refund_status). NULL means nothing
    # is owed. This is the merchant-visibility surface for that case.
    refund_status: Optional[str] = None


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
