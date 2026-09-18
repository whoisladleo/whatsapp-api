import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.provider import WhatsAppProvider
from app.crud import message as crud_message
from app.crud.order import (
    OrderNotCancellableError,
    cancel_order,
    get_order_detail_for_merchant,
    get_order_for_merchant,
    get_orders_by_merchant,
)
from app.db.models import OrderStatus, User, WhatsAppAccount
from app.db.session import get_db
from app.schemas.order import OrderDetail, OrderRead

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=List[OrderRead])
def list_orders(
    status: Optional[OrderStatus] = None,
    account_id: Optional[uuid.UUID] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return get_orders_by_merchant(
        db, current_user.id, status=status, account_id=account_id, skip=skip, limit=limit
    )


@router.get("/{order_id}", response_model=OrderDetail)
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = get_order_detail_for_merchant(db, order_id, current_user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/{order_id}/payment-history")
async def get_order_payment_history(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Live pass-through of Tunzaa's own payment_history for an installment
    order — deliberately not persisted locally (Tunzaa stays the source of
    truth for individual payment events, same principle as
    paid_amount/remaining_balance). Returned as-is/untyped: this project
    has only ever observed an empty payment_history array in testing (no
    real payment has been made against a sandbox order), so the shape of a
    populated entry isn't confirmed and isn't worth guessing at with a
    strict schema.
    """
    order = get_order_for_merchant(db, order_id, current_user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.tunzaa_plan_id:
        raise HTTPException(
            status_code=400,
            detail="This order has no associated installment plan.",
        )

    from app.services.tunzaa import tunzaa_service

    try:
        plan_data = await tunzaa_service.get_installment_plan(
            order.tunzaa_plan_id, include_payments=True
        )
    except Exception as e:
        logger.error(f"Failed to fetch payment history for order {order_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not reach Tunzaa right now.")

    return plan_data.get("payment_history", [])


@router.post("/{order_id}/cancel", response_model=OrderDetail)
async def cancel_existing_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = get_order_for_merchant(db, order_id, current_user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        cancel_order(db, order)
    except OrderNotCancellableError:
        raise HTTPException(
            status_code=400,
            detail=f"Order is {order.status.value}, not PENDING — it can no longer be cancelled this way.",
        )

    await _notify_customer_of_cancellation(db, order)

    return get_order_detail_for_merchant(db, order_id, current_user.id)


async def _notify_customer_of_cancellation(db: Session, order) -> None:
    if not order.account_id or not order.customer:
        return
    account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()
    if not account:
        return

    body = (
        "Your order has been cancelled by the merchant and the item has "
        "been released. Message us if you have any questions."
    )
    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(order.customer.phone_number, body)
    except Exception as e:
        logger.error(f"Failed to notify customer of order cancellation: {e}")
        resp = None

    crud_message.create_message(
        db,
        {
            "to_phone": order.customer.phone_number,
            "type": "text",
            "body": body,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )
