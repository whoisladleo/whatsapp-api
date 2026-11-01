import uuid
from typing import List, Optional

from sqlalchemy import func, or_
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
        channel=(
            "instagram"
            if order.instagram_account_id
            else ("whatsapp" if order.account_id else "dashboard")
        ),
        product_name=order.product.name if order.product else None,
        customer_phone=order.customer.phone_number if order.customer else None,
        reserved_until=order.reserved_until,
        created_at=order.created_at,
        subscription_status=order.subscription.status if order.subscription else None,
        subscription_next_billing_at=order.subscription.next_billing_at if order.subscription else None,
        subscription_retry_count=order.subscription.retry_count if order.subscription else None,
        refund_status=order.refund_status,
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
    user_id: uuid.UUID,
    status: Optional[OrderStatus] = None,
    account_id: Optional[uuid.UUID] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[OrderRead]:
    # A merchant's order can belong to either a WhatsAppAccount or an
    # InstagramAccount (never both — see Order.instagram_account_id), so
    # this outer-joins both and matches whichever one is actually set,
    # rather than requiring every merchant to have a WhatsApp account.
    # Dashboard-native purchases (Pay Now) carry no channel account at
    # all — those match on the recorded buying user instead (#16).
    query = (
        db.query(Order)
        .outerjoin(WhatsAppAccount, Order.account_id == WhatsAppAccount.id)
        .outerjoin(InstagramAccount, Order.instagram_account_id == InstagramAccount.id)
        .filter(
            or_(
                WhatsAppAccount.user_id == user_id,
                InstagramAccount.user_id == user_id,
                Order.buyer_user_id == user_id,
            )
        )
    )
    if status:
        query = query.filter(Order.status == status)
    if account_id:
        query = query.filter(Order.account_id == account_id)

    orders = query.order_by(Order.created_at.desc()).offset(skip).limit(limit).all()
    return [_to_order_read(o) for o in orders]


def get_order_for_merchant(db: Session, order_id: uuid.UUID, user_id: uuid.UUID) -> Optional[Order]:
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
                Order.buyer_user_id == user_id,
            ),
        )
        .first()
    )


def get_order_detail_for_merchant(db: Session, order_id: uuid.UUID, user_id: uuid.UUID) -> Optional[OrderDetail]:
    order = get_order_for_merchant(db, order_id, user_id)
    return _to_order_detail(order) if order else None


def try_release_pending_order(
    db: Session, order: Order, to_status: OrderStatus
) -> tuple[bool, Optional[uuid.UUID]]:
    """
    Atomically moves a PENDING order to to_status (FAILED/CANCELLED) and
    restores its stock reservation — the release-side mirror of
    _try_claim_stock: a conditional UPDATE decides the PENDING->?
    transition, so a FAILED callback racing auto-cancel (or a dashboard
    cancel, or the reclose guard's lazy retire of an expired hold) can't
    both restore the same unit (+2 phantom stock, P10). Only the
    transition winner restores, and the restore itself is a SQL expression
    (quantity = quantity + N), not a read-modify-write. Returns
    (won, restored_product_id): won=False means the order already left
    PENDING — a concurrent path won and restored — so the caller must
    treat the order as already resolved and restore nothing. Mutates
    without committing — the caller commits once.
    """
    moved = (
        db.query(Order)
        .filter(Order.id == order.id, Order.status == OrderStatus.PENDING)
        .update({Order.status: to_status}, synchronize_session=False)
    )
    if moved == 0:
        return False, None
    order.status = to_status
    if not order.product_id:
        return True, None
    product = db.query(Product).filter(Product.id == order.product_id).first()
    # Subscriptions never claim stock at close, so there is nothing
    # to give back — restoring would mint phantom units (#118).
    if product is None or product.is_subscription:
        return True, None
    units = order.quantity or 1
    db.query(Product).filter(Product.id == order.product_id).update(
        {Product.quantity: func.coalesce(Product.quantity, 0) + units},
        synchronize_session=False,
    )
    return True, product.id


def cancel_order(db: Session, order: Order) -> None:
    """
    Cancels a PENDING order and releases its stock reservation. Raises
    OrderNotCancellableError if the order has already moved past PENDING
    (paid orders need a refund flow, not a plain cancel — out of scope here).
    Does not notify the customer; the caller (app/api/v1/orders.py) does
    that, matching the pattern already established in tunzaa_webhook.py.
    """
    won, restored_product_id = try_release_pending_order(db, order, OrderStatus.CANCELLED)
    if not won:
        raise OrderNotCancellableError()
    db.commit()
    if restored_product_id:
        # The release may flip the catalog's availability back to in stock.
        from app.tasks.catalog_sync import dispatch_catalog_sync
        dispatch_catalog_sync(restored_product_id, "upsert")
