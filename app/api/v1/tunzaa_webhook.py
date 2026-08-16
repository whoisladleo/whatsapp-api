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
    Order,
    OrderStatus,
    Product,
    WhatsAppAccount,
)
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


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

    order = db.query(Order).filter(Order.id == int(order_ref.split("-")[-1])).first() if "-" in order_ref else None

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

        return {"status": "success", "message": f"Order status: {order.status.value}"}

    elif status_value in ("FAILED", "CANCELLED"):
        order.status = OrderStatus.FAILED
        if transaction_id:
            order.tunzaa_transaction_id = transaction_id
        _release_reservation(db, order)
        db.commit()

        await _notify_customer(db, order, "payment_failed")

        return {"status": "success", "message": "Order marked as FAILED"}

    return {"status": "ignored", "reason": "Unhandled status"}


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


async def _notify_customer(db: Session, order: Order, kind: str) -> None:
    if not order.account_id or not order.customer:
        return
    account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()
    if not account:
        return

    if kind == "payment_complete":
        body = (
            "🎉 Payment confirmed! Thank you for your order. "
            "We'll be in touch shortly about delivery."
        )
    else:
        body = (
            "Your payment could not be completed, so this order has been "
            "cancelled and the item released. Message us again if you'd "
            "like to try again."
        )

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
