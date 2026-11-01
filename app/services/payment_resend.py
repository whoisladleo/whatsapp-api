import enum
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.crud import message as crud_message
from app.db.models import (
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    OrderStatus,
    PaymentType,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)


class ResendOutcome(enum.Enum):
    RESENT = "resent"  # prompt re-fired on the same live order
    THROTTLED = "throttled"  # re-fired too recently; retry_after_seconds set
    INSTALLMENT_REFUSED = "installment_refused"  # legs are Tunzaa-pushed, never re-fired
    TUNZAA_ERROR = "tunzaa_error"  # initiate call failed; order untouched
    FRESH_ORDERED = "fresh_ordered"  # expired hold: old row cancelled, new order dispatched
    OUT_OF_STOCK = "out_of_stock"  # fresh-clone failed: no units left
    DEAL_INVALID = "deal_invalid"  # fresh-clone failed: product gone/draft/repriced
    ALREADY_OWNED = "already_owned"  # fresh-clone blocked: subscription already paid
    NO_PHONE = "no_phone"  # IG order with no phone-keyed customer to prompt
    NOT_PENDING = "not_pending"  # defensive: caller passed a settled order


@dataclass
class ResendResult:
    outcome: ResendOutcome
    order: Optional[Order] = None  # the order awaiting payment (new row for FRESH_ORDERED)
    retry_after_seconds: Optional[int] = None
    detail: Optional[str] = None  # safe merchant-facing explanation, no internals


async def resend_live_order(db: Session, order: Order, phone: str) -> ResendResult:
    """
    Re-fires the Tunzaa prompt on a live PENDING FULL order, or dispatches
    it for the first time when the original close never got that far. Same
    order-<id> reference, so an approval of any outstanding prompt
    completes the same order — newest transaction id wins, no
    double-charge by construction. Throttled (see RESEND_MIN_INTERVAL).
    Shared by the RESEND_PAYMENT marker path, the /resend merchant
    command, and the dashboard resend button (#113, #117).
    """
    from app.services.tunzaa import tunzaa_service

    if order.payment_type != PaymentType.FULL:
        return ResendResult(ResendOutcome.INSTALLMENT_REFUSED, order=order)

    from app.services.llm import RESEND_MIN_INTERVAL

    if order.tunzaa_transaction_id and order.updated_at:
        elapsed = datetime.utcnow() - order.updated_at
        if elapsed < RESEND_MIN_INTERVAL:
            retry_after = int((RESEND_MIN_INTERVAL - elapsed).total_seconds())
            return ResendResult(ResendOutcome.THROTTLED, order=order, retry_after_seconds=retry_after)

    try:
        resp = await tunzaa_service.initiate_payment(
            phone=phone, amount=order.agreed_price, reference=f"order-{order.id}"
        )
    except Exception as e:
        logger.error(f"Tunzaa resend failed for order {order.id}: {e}")
        return ResendResult(ResendOutcome.TUNZAA_ERROR, order=order)
    order.tunzaa_transaction_id = resp.get("transactionID")
    order.updated_at = datetime.utcnow()
    db.commit()
    return ResendResult(ResendOutcome.RESENT, order=order)


def _channel_parties(db: Session, order: Order):
    """
    Resolves (account, customer, context, phone, is_instagram) for a
    merchant resend. WhatsApp orders key off the phone-number Customer;
    Instagram orders off the InstagramCustomer, with the prompt going to
    the phone-keyed Customer row the IG close created alongside.
    Returns None when the order's channel rows are gone.
    """
    if order.instagram_account_id:
        account = (
            db.query(InstagramAccount).filter(InstagramAccount.id == order.instagram_account_id).first()
        )
        ig_customer = (
            db.query(InstagramCustomer).filter(InstagramCustomer.id == order.instagram_customer_id).first()
            if order.instagram_customer_id
            else None
        )
        if not account or not ig_customer:
            return None
        context = (
            db.query(InstagramConversationContext)
            .filter(InstagramConversationContext.customer_id == ig_customer.id)
            .first()
        )
        if not context:
            context = InstagramConversationContext(customer_id=ig_customer.id)
            db.add(context)
            db.commit()
            db.refresh(context)
        phone_customer = (
            db.query(Customer).filter(Customer.id == order.customer_id).first()
            if order.customer_id
            else None
        )
        phone = phone_customer.phone_number if phone_customer else None
        return (account, ig_customer, context, phone, True)

    account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()
    customer = db.query(Customer).filter(Customer.id == order.customer_id).first()
    if not account or not customer:
        return None
    context = crud_message.get_or_create_context(db, customer.id, account.id)
    return (account, customer, context, customer.phone_number, False)


async def close_deal_from_terms(db: Session, parties, product, price) -> tuple:
    """
    Shared close-from-known-terms tail for _fresh_clone (#117) and
    phantom auto-recovery (#120): runs the real close path with a
    synthetic FULL agreement, then proves a live PENDING row landed —
    guards can return a note instead of ordering (PAID subscription
    history blocks silently by design, #114), and races can lose.
    parties is the _channel_parties tuple
    (account, customer, context, phone, is_instagram).

    Returns (new_order, None) on success, (None, reason) otherwise.
    Reasons: "unavailable" (no closer) | "out_of_stock" |
    "deal_invalid" | "tunzaa_error" | "already_owned" (blocked with no
    live row behind it). Never touches any pre-existing row — callers
    own cancel/restore around it.
    """
    from app.services.llm import DealRejected, _hold_expired, llm_service

    if llm_service is None:
        return None, "unavailable"

    account, customer, context, phone, is_instagram = parties
    agreement = {
        "product_name": product.name,
        "price": price,
        "payment_type": "full",
        "phone": phone if is_instagram else None,
    }
    try:
        if is_instagram:
            await llm_service._close_instagram_deal(db, account, customer, agreement)
        else:
            await llm_service._close_deal(db, account, customer, phone, agreement, context)
    except DealRejected as e:
        logger.warning(f"Close-from-terms rejected for {product.name}: {e}")
        if "out of stock" in str(e):
            return None, "out_of_stock"
        return None, "deal_invalid"
    except Exception as e:
        logger.error(f"Close-from-terms crashed for {product.name}: {e}")
        return None, "tunzaa_error"

    channel_filter = (
        Order.instagram_account_id == account.id
        if is_instagram
        else Order.account_id == account.id
    )
    customer_filter = (
        Order.instagram_customer_id == customer.id
        if is_instagram
        else Order.customer_id == customer.id
    )
    fresh = (
        db.query(Order)
        .filter(
            customer_filter,
            Order.product_id == product.id,
            channel_filter,
            Order.status == OrderStatus.PENDING,
            Order.payment_type == PaymentType.FULL,
        )
        .order_by(Order.created_at.desc())
        .first()
    )
    if fresh is None or _hold_expired(fresh):
        return None, "already_owned"
    return fresh, None


async def _fresh_clone(db: Session, order: Order, parties) -> ResendResult:
    """
    Expired hold, merchant-approved retry: cancel the lapsed row, then
    re-close the identical deal (same product, same price, FULL) through
    the real close path — catalog/price/stock validation, #114 race
    protection, subscription re-pointing, reminder scheduling, and
    dispatch all come along. Cancel-before-close satisfies the
    one-pending-per-deal index; on any failure the old row is restored
    exactly (status PENDING, hold untouched) plus its stock adjustment
    reversed, so a failed retry leaves history truthful.
    """
    account, customer, context, phone, is_instagram = parties
    if not phone:
        return ResendResult(ResendOutcome.NO_PHONE, order=order)
    product = order.product
    if not product or product.is_draft:
        return ResendResult(
            ResendOutcome.DEAL_INVALID, order=order,
            detail=f"'{product.name if product else 'this product'}' is no longer available.",
        )
    if not (product.min_price <= (order.agreed_price or 0) <= product.max_price):
        return ResendResult(
            ResendOutcome.DEAL_INVALID, order=order,
            detail="The agreed price no longer matches the catalog — re-negotiate before retrying.",
        )
    # Pre-flight the stock check (goods only — subscriptions have no
    # units): the atomic claim inside the close is the real guard, but
    # failing here avoids churning the old row when the shelf is
    # visibly empty.
    if not product.is_subscription and (product.quantity or 0) < 1:
        return ResendResult(
            ResendOutcome.OUT_OF_STOCK, order=order,
            detail=f"'{product.name}' is out of stock.",
        )

    saved_status = order.status
    saved_hold = order.reserved_until
    stock_restored = False
    order.status = OrderStatus.CANCELLED
    if not product.is_subscription:
        product.quantity = (product.quantity or 0) + (order.quantity or 1)
        stock_restored = True
    db.commit()

    def _restore(result: ResendResult) -> ResendResult:
        order.status = saved_status
        order.reserved_until = saved_hold
        if stock_restored:
            product.quantity = (product.quantity or 0) - (order.quantity or 1)
        db.commit()
        return result

    fresh, reason = await close_deal_from_terms(db, parties, product, order.agreed_price)
    if fresh is not None:
        return ResendResult(ResendOutcome.FRESH_ORDERED, order=fresh)
    if reason == "out_of_stock":
        return _restore(ResendResult(
            ResendOutcome.OUT_OF_STOCK, order=order, detail=f"'{product.name}' is out of stock."
        ))
    if reason == "deal_invalid":
        return _restore(ResendResult(
            ResendOutcome.DEAL_INVALID, order=order,
            detail="The deal is no longer valid (product or price changed).",
        ))
    if reason == "already_owned":
        return _restore(ResendResult(
            ResendOutcome.ALREADY_OWNED, order=order,
            detail="This plan is already paid for — no new prompt was sent.",
        ))
    if reason == "unavailable":
        return _restore(ResendResult(ResendOutcome.TUNZAA_ERROR, order=order, detail="Closer unavailable."))
    return _restore(ResendResult(ResendOutcome.TUNZAA_ERROR, order=order, detail="Could not reach Tunzaa right now."))


async def merchant_resend_order(db: Session, order: Order) -> ResendResult:
    """
    One guarded entry point for human-triggered retries (the /resend
    merchant command and POST /orders/{id}/resend, #117): live PENDING
    FULL orders re-fire in place, expired ones re-close from identical
    terms, installments are refused (Tunzaa pushes legs on its own
    schedule — re-firing would rebill one). The caller renders the
    outcome; ResendResult.detail is always safe to show a merchant.
    """
    from app.services.llm import _hold_expired

    if order.status != OrderStatus.PENDING:
        return ResendResult(ResendOutcome.NOT_PENDING, order=order)
    if order.payment_type != PaymentType.FULL:
        return ResendResult(
            ResendOutcome.INSTALLMENT_REFUSED, order=order,
            detail="Installment prompts go out on Tunzaa's own schedule — there is nothing to resend.",
        )
    parties = _channel_parties(db, order)
    if parties is None:
        return ResendResult(
            ResendOutcome.DEAL_INVALID, order=order,
            detail="This order's channel is gone — it can't be retried.",
        )
    _account, _customer, _context, phone, _is_ig = parties
    if not phone:
        return ResendResult(
            ResendOutcome.NO_PHONE, order=order,
            detail="No phone number on file for this customer — nothing to send the prompt to.",
        )
    if _hold_expired(order):
        return await _fresh_clone(db, order, parties)
    return await resend_live_order(db, order, phone)
