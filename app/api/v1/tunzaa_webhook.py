import uuid
from fastapi import APIRouter, Depends, Header, Request, HTTPException
from typing import Optional
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.security import verify_tunzaa_signature
from app.core.provider import WhatsAppProvider
from app.crud import message as crud_message
from app.db.session import get_db
from app.db.models import (
    Installment,
    InstallmentStatus,
    NumberSubscription,
    Order,
    OrderStatus,
    Product,
    ProductSubscription,
    WhatsAppAccount,
)
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_uuid_suffix(ref: str, prefix: str) -> Optional[uuid.UUID]:
    """
    Reference ids are built as f"{prefix}{model.id}" (e.g. "order-<uuid>").
    IDs are now UUIDs, which themselves contain hyphens, so the id must be
    parsed by stripping the known prefix rather than the old
    ref.split("-")[-1] — that only grabbed the last 12 hex chars of a UUID
    and would never parse as one.
    """
    try:
        return uuid.UUID(ref[len(prefix):])
    except ValueError:
        return None


@router.post("/tunzaa-webhook")
async def handle_tunzaa_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
):
    """
    Webhook endpoint receiving Tunzaa/Malipo payment callbacks. Real payload
    shape (per Tunzaa's own webhook docs — NOT the event/reference fields
    this handler used to read, which never matched anything Tunzaa actually
    sends):

        {"transaction_id": "...", "status": "COMPLETED" | "FAILED" | "CANCELLED",
         "reference_id": "order-<id>", "amount": "...", "payment_date": "...",
         "timestamp": "..."}

    Every real callback is signed via an X-Signature header (HMAC-SHA256 of
    the payload re-serialized with alphabetically sorted keys). This moves
    real money, so unlike the more lenient Meta-webhook precedent
    (app/api/v1/whatsapp.py), an unsigned/mismatched request is always
    rejected whenever TUNZAA_SECRET_KEY is configured — verification is only
    skipped (with a loud warning) when the secret genuinely isn't set, e.g.
    local dev.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if settings.TUNZAA_SECRET_KEY:
        if not verify_tunzaa_signature(data, x_signature):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        logger.warning(
            "TUNZAA_SECRET_KEY is not configured; skipping Tunzaa webhook "
            "signature verification. Set it to prevent spoofed payment "
            "confirmations."
        )

    logger.info(f"Received Tunzaa Webhook: {data}")

    status_value = data.get("status")
    order_ref = data.get("reference_id")
    transaction_id = data.get("transaction_id")

    if not order_ref:
        return {"status": "ignored", "reason": "No reference_id provided"}

    if order_ref.startswith("number-purchase-") or order_ref.startswith("number-renewal-"):
        return await _handle_number_subscription_webhook(db, order_ref, status_value, transaction_id)

    if order_ref.startswith("product-subscription-renewal-"):
        return await _handle_product_subscription_renewal_webhook(db, order_ref, status_value, transaction_id)

    order_id = _parse_uuid_suffix(order_ref, "order-")
    order = db.query(Order).filter(Order.id == order_id).first() if order_id else None

    if not order:
        return {"status": "ignored", "reason": "Order not found"}

    # Idempotency: Tunzaa retries a callback up to 5 times per their docs.
    # Once we've recorded a transaction_id as processed for a
    # no-longer-PENDING order, treat a repeat as already handled.
    if (
        transaction_id
        and order.tunzaa_transaction_id
        and order.tunzaa_transaction_id == transaction_id
        and order.status != OrderStatus.PENDING
    ):
        return {"status": "ignored", "reason": "Already processed"}

    if status_value == "COMPLETED":
        if transaction_id:
            order.tunzaa_transaction_id = transaction_id

        installment_id = data.get("installment_id")
        if installment_id and order.tunzaa_plan_id:
            # Per-leg idempotency: Tunzaa retries a callback up to 5 times
            # per their docs. The order-level check above only catches a
            # retry once order.status leaves PENDING, which never happens
            # for a non-final leg of a multi-installment plan (the order
            # correctly stays PENDING until the whole plan is paid off) —
            # without this, a retried callback for an already-processed leg
            # would re-send the customer's (and merchant's) payment
            # notification for the same real payment. See #31.
            already_processed_leg = (
                db.query(Installment)
                .filter(
                    Installment.order_id == order.id,
                    Installment.tunzaa_installment_id == installment_id,
                    Installment.status == InstallmentStatus.PAID,
                )
                .first()
            )
            if already_processed_leg:
                return {"status": "ignored", "reason": "Installment already processed"}

            # One leg of a multi-installment plan. Tunzaa tracks cumulative
            # payment at the plan level (paid_amount/remaining_balance), not
            # just a binary paid/unpaid per installment — reconcile against
            # that authoritative state instead of assuming this one callback
            # means the whole order is paid. This is what correctly absorbs
            # an over/under-paid installment: whatever Tunzaa's ledger says
            # is still owed drives our Order.status, not this callback alone.
            await _reconcile_installment_plan(db, order, installment_id)
        else:
            # One-off FULL payment (no installment_id), or an
            # installment order with no captured plan id to reconcile
            # against (e.g. pre-existing data) — no plan-level balance to
            # check, so this single callback is authoritative on its own.
            order.status = OrderStatus.PAID
            if installment_id:
                _mark_single_installment_paid(db, order, installment_id)

        db.commit()

        # Stock was already decremented at reservation time (order creation
        # in app/services/llm.py) — nothing further to do there. Just
        # confirm to the customer.
        await _notify_customer(db, order, "payment_complete")
        await _activate_subscription_if_linked(db, order)

        return {"status": "success", "message": f"Order status: {order.status.value}"}

    elif status_value in ("FAILED", "CANCELLED"):
        order.status = OrderStatus.FAILED
        if transaction_id:
            order.tunzaa_transaction_id = transaction_id
        _release_reservation(db, order)
        db.commit()

        await _notify_customer(db, order, "payment_failed")
        _fail_subscription_if_linked(db, order)

        return {"status": "success", "message": "Order marked as FAILED"}

    return {"status": "ignored", "reason": "Unhandled status"}


async def _handle_number_subscription_webhook(
    db: Session, order_ref: str, status_value: Optional[str], transaction_id: Optional[str]
) -> dict:
    """Routes a "number-purchase-<id>" / "number-renewal-<id>" callback to
    app/services/number_billing.py rather than the Order-shaped handling
    below it, which these NumberSubscription rows have nothing to do with."""
    from app.services import number_billing

    prefix = "number-purchase-" if order_ref.startswith("number-purchase-") else "number-renewal-"
    subscription_id = _parse_uuid_suffix(order_ref, prefix)
    if subscription_id is None:
        return {"status": "ignored", "reason": "Malformed reference_id"}

    subscription = (
        db.query(NumberSubscription).filter(NumberSubscription.id == subscription_id).first()
    )
    if not subscription:
        return {"status": "ignored", "reason": "Subscription not found"}

    # Idempotency: Tunzaa retries a callback up to 5 times per their docs —
    # the handlers below are what set tunzaa_transaction_id, so a repeat of
    # a transaction_id already recorded means this exact event already ran.
    if transaction_id and subscription.tunzaa_transaction_id == transaction_id:
        return {"status": "ignored", "reason": "Already processed"}

    if order_ref.startswith("number-purchase-"):
        await number_billing.handle_purchase_payment_result(db, subscription, status_value, transaction_id)
    else:
        await number_billing.handle_renewal_payment_result(db, subscription, status_value, transaction_id)

    return {
        "status": "success",
        "message": f"Subscription {subscription.id} status: {subscription.status.value}",
    }


async def _handle_product_subscription_renewal_webhook(
    db: Session, order_ref: str, status_value: Optional[str], transaction_id: Optional[str]
) -> dict:
    """Routes a "product-subscription-renewal-<id>" callback to
    app/services/product_subscription.py. The *first* charge for a
    subscription product is a normal "order-<id>" reference handled by the
    Order-shaped logic above (see _activate_subscription_if_linked /
    _fail_subscription_if_linked) — only renewals from month 2 onward use
    this prefix."""
    from app.services import product_subscription

    subscription_id = _parse_uuid_suffix(order_ref, "product-subscription-renewal-")
    if subscription_id is None:
        return {"status": "ignored", "reason": "Malformed reference_id"}

    subscription = (
        db.query(ProductSubscription).filter(ProductSubscription.id == subscription_id).first()
    )
    if not subscription:
        return {"status": "ignored", "reason": "Subscription not found"}

    if transaction_id and subscription.tunzaa_transaction_id == transaction_id:
        return {"status": "ignored", "reason": "Already processed"}

    await product_subscription.handle_renewal_payment_result(db, subscription, status_value, transaction_id)

    return {
        "status": "success",
        "message": f"Subscription {subscription.id} status: {subscription.status.value}",
    }


async def _activate_subscription_if_linked(db: Session, order: Order) -> None:
    from app.db.models import InstallmentMode, ProductSubscriptionStatus
    from app.services import product_subscription

    subscription = (
        db.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    )
    if not subscription or subscription.status != ProductSubscriptionStatus.PENDING_PAYMENT:
        return

    # SNPL withholds activation until the installment plan is fully paid
    # off (order.status only reaches PAID once remaining_balance <= 0 —
    # see _reconcile_installment_plan). BNPL (the default, matching
    # pre-existing behavior) activates on this first successful payment
    # regardless of the order's own status, same as a FULL payment always
    # has. A FULL payment's order.status is already PAID by the time this
    # runs either way, so this check is a no-op for it.
    product = order.product
    if (
        product
        and product.installment_mode == InstallmentMode.SNPL
        and order.status != OrderStatus.PAID
    ):
        return

    product_subscription.activate_after_first_payment(db, subscription)


def _fail_subscription_if_linked(db: Session, order: Order) -> None:
    from app.db.models import ProductSubscriptionStatus
    from app.services import product_subscription

    subscription = (
        db.query(ProductSubscription).filter(ProductSubscription.order_id == order.id).first()
    )
    if subscription and subscription.status == ProductSubscriptionStatus.PENDING_PAYMENT:
        product_subscription.mark_first_payment_failed(db, subscription)


def _mark_single_installment_paid(db: Session, order: Order, installment_id) -> None:
    installment = db.query(Installment).filter(
        Installment.order_id == order.id,
        Installment.tunzaa_installment_id == installment_id,
    ).first()
    if installment:
        installment.status = InstallmentStatus.PAID


async def _reconcile_installment_plan(db: Session, order: Order, installment_id) -> None:
    """
    Fetches the plan's authoritative state from Tunzaa (paid_amount/
    remaining_balance at the plan level, plus each installment's own
    status) and syncs our local rows from it, rather than trusting this
    one callback's fields in isolation. Falls back to the simpler
    single-installment-matching behavior (and marks the order PAID
    outright, matching this handler's pre-reconciliation behavior) if the
    Tunzaa call itself fails, so a transient network error doesn't leave
    the order stuck.
    """
    from app.services.tunzaa import tunzaa_service

    try:
        plan_data = await tunzaa_service.get_installment_plan(
            order.tunzaa_plan_id, include_payments=True
        )
    except Exception as e:
        logger.warning(
            f"get_installment_plan failed for order {order.id} "
            f"(plan {order.tunzaa_plan_id}); falling back to single-"
            f"installment matching: {e}"
        )
        order.status = OrderStatus.PAID
        _mark_single_installment_paid(db, order, installment_id)
        return

    for inst_data in plan_data.get("installments", []):
        remote_id = inst_data.get("installment_id")
        if remote_id is None:
            continue
        local = db.query(Installment).filter(
            Installment.order_id == order.id,
            Installment.tunzaa_installment_id == str(remote_id),
        ).first()
        if not local:
            continue
        remote_status = (inst_data.get("status") or "").upper()
        if remote_status == "PAID":
            local.status = InstallmentStatus.PAID
        elif remote_status == "OVERDUE":
            local.status = InstallmentStatus.OVERDUE

    plan = plan_data.get("plan", {})
    order.paid_amount = _parse_float(plan.get("paid_amount"))
    order.remaining_balance = _parse_float(plan.get("remaining_balance"))

    if order.remaining_balance is not None and order.remaining_balance <= 0:
        order.status = OrderStatus.PAID
    # else: leave order PENDING — the plan still has a balance outstanding,
    # regardless of whether this particular leg was paid exactly, under, or
    # over what was due.


def _parse_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _release_reservation(db: Session, order: Order) -> None:
    """Restores stock reserved at order-creation time for a failed/cancelled order."""
    if not order.product_id:
        return
    product = db.query(Product).filter(Product.id == order.product_id).first()
    if product:
        product.quantity += order.quantity or 1


def _payment_notification_body(kind: str) -> str:
    if kind == "payment_complete":
        return (
            "🎉 Payment confirmed! Thank you for your order. "
            "We'll be in touch shortly about delivery."
        )
    return (
        "Your payment could not be completed, so this order has been "
        "cancelled and the item released. Message us again if you'd "
        "like to try again."
    )


async def _notify_customer(db: Session, order: Order, kind: str) -> None:
    """
    Routes by whichever channel the order actually closed on. An
    Instagram-originated order has no account_id (instagram_account_id is
    set instead — see Order.instagram_customer_id) — this used to only
    check account_id and silently never reach an Instagram customer at
    all, confirmed live: a real customer was never told their payment
    succeeded. See #31.
    """
    if order.account_id:
        await _notify_customer_whatsapp(db, order, kind)
    elif order.instagram_account_id:
        await _notify_customer_instagram(db, order, kind)
    # else: no channel on file for this order — nothing to notify.


async def _notify_customer_whatsapp(db: Session, order: Order, kind: str) -> None:
    if not order.customer:
        return
    account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()
    if not account:
        return

    body = _payment_notification_body(kind)

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(order.customer.phone_number, body)
    except Exception as e:
        logger.error(f"Failed to send Tunzaa payment notification to customer: {e}")
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


async def _notify_customer_instagram(db: Session, order: Order, kind: str) -> None:
    from app.core.instagram_provider import InstagramProvider
    from app.crud import instagram as crud_instagram

    ig_customer = order.instagram_customer
    account = order.instagram_account
    if not ig_customer or not account:
        return

    body = _payment_notification_body(kind)

    provider = InstagramProvider(account.access_token)
    try:
        resp = await provider.send_direct_message(account.ig_business_id, ig_customer.ig_scoped_id, body)
    except Exception as e:
        logger.error(f"Failed to send Tunzaa payment notification (Instagram) to customer: {e}")
        return

    if "error" in resp:
        logger.error(f"Instagram payment notification failed for order {order.id}: {resp}")
        return

    crud_instagram.create_direct_message(
        db,
        account_id=account.id,
        customer_id=ig_customer.id,
        direction="outbound",
        body=body,
        ig_message_id=resp.get("message_id"),
    )
