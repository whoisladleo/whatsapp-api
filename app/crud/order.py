from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import InstagramAccount, Order, OrderStatus, Product, WhatsAppAccount
from app.schemas.order import OrderDetail, OrderRead


class OrderNotCancellableError(Exception):
    """Raised when trying to cancel an order that isn't PENDING."""


class OrderHasNoPaymentPlanError(Exception):
    """Raised when payment history is requested for an order with no tunzaa_plan_id (not an installment order, or predates plan-id capture)."""


def _to_order_read(order: Order) -> OrderRead:
    return OrderRead(
        id=order.id,
        status=order.status,
        payment_type=order.payment_type,
        quantity=order.quantity,
        agreed_price=order.agreed_price,
        account_id=order.account_id,
        instagram_account_id=order.instagram_account_id,
        channel="instagram" if order.instagram_account_id else "whatsapp",
        product_name=order.product.name if order.product else None,
        customer_phone=order.customer.phone_number if order.customer else None,
        reserved_until=order.reserved_until,
        created_at=order.created_at,
    )


def _to_order_detail(order: Order) -> OrderDetail:
    base = _to_order_read(order)
    percentage_complete = None
    if order.paid_amount is not None and order.agreed_price:
        percentage_complete = round((order.paid_amount / order.agreed_price) * 100, 2)

    return OrderDetail(
        **base.model_dump(),
        delivery_address=order.customer.delivery_address if order.customer else None,
        tunzaa_transaction_id=order.tunzaa_transaction_id,
        installments=order.installments,
        paid_amount=order.paid_amount,
        remaining_balance=order.remaining_balance,
        percentage_complete=percentage_complete,
    )


def get_orders_by_merchant(
    db: Session,
    user_id: int,
    status: Optional[OrderStatus] = None,
    account_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[OrderRead]:
    # A merchant's order can belong to either a WhatsAppAccount or an
    # InstagramAccount (never both — see Order.instagram_account_id), so
    # this outer-joins both and matches whichever one is actually set,
    # rather than requiring every merchant to have a WhatsApp account.
    query = (
        db.query(Order)
        .outerjoin(WhatsAppAccount, Order.account_id == WhatsAppAccount.id)
        .outerjoin(InstagramAccount, Order.instagram_account_id == InstagramAccount.id)
        .filter(
            or_(
                WhatsAppAccount.user_id == user_id,
                InstagramAccount.user_id == user_id,
            )
        )
    )
    if status:
        query = query.filter(Order.status == status)
    if account_id:
        query = query.filter(Order.account_id == account_id)

    orders = query.order_by(Order.created_at.desc()).offset(skip).limit(limit).all()
    return [_to_order_read(o) for o in orders]


def get_order_for_merchant(db: Session, order_id: int, user_id: int) -> Optional[Order]:
    """Returns the raw ORM Order (not yet serialized) scoped to the merchant, or None."""
    return (
        db.query(Order)
        .outerjoin(WhatsAppAccount, Order.account_id == WhatsAppAccount.id)
        .outerjoin(InstagramAccount, Order.instagram_account_id == InstagramAccount.id)
        .filter(
            Order.id == order_id,
            or_(
                WhatsAppAccount.user_id == user_id,
                InstagramAccount.user_id == user_id,
            ),
        )
        .first()
    )


def get_order_detail_for_merchant(db: Session, order_id: int, user_id: int) -> Optional[OrderDetail]:
    order = get_order_for_merchant(db, order_id, user_id)
    return _to_order_detail(order) if order else None


def cancel_order(db: Session, order: Order) -> None:
    """
    Cancels a PENDING order and releases its stock reservation. Raises
    OrderNotCancellableError if the order has already moved past PENDING
    (paid orders need a refund flow, not a plain cancel — out of scope here).
    Does not notify the customer; the caller (app/api/v1/orders.py) does
    that, matching the pattern already established in tunzaa_webhook.py.
    """
    if order.status != OrderStatus.PENDING:
        raise OrderNotCancellableError()

    order.status = OrderStatus.CANCELLED
    if order.product_id:
        product = db.query(Product).filter(Product.id == order.product_id).first()
        if product:
            product.quantity += order.quantity or 1
    db.commit()
