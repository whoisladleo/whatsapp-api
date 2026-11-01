import uuid
from fastapi import APIRouter, Depends, Header, Request, HTTPException
from typing import Optional
from sqlalchemy.orm import Session
from app.services.llm import _lang_text
from app.core.config import settings
from app.core.security import verify_tunzaa_signature
from app.core.provider import WhatsAppProvider
from app.crud import message as crud_message
from app.services import usage
from app.db.session import get_db
from app.db.models import (
    AmaraEventKind,
    Installment,
    InstallmentStatus,
    NumberSubscription,
    Order,
    OrderStatus,
    PaymentType,
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
    the payload re-serialized with alphabetically sorted keys). An
    unsigned/mismatched request is always rejected whenever
    TUNZAA_SECRET_KEY is configured; with no secret, requests are still
    rejected unless ALLOW_UNSIGNED_WEBHOOKS is explicitly true (local dev).
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if settings.TUNZAA_SECRET_KEY:
        if not verify_tunzaa_signature(data, x_signature):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    elif not settings.ALLOW_UNSIGNED_WEBHOOKS:
        raise HTTPException(status_code=403, detail="Webhook signature verification is not configured")
    else:
        logger.warning(
            "TUNZAA_SECRET_KEY is not configured and ALLOW_UNSIGNED_WEBHOOKS=true; "
            "accepting unsigned payment confirmations."
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
        if order.status == OrderStatus.PAID:
            # A second COMPLETED with a new transaction_id (genuine double
            # payment, not a retry — retries share the id and are caught
            # above). The cash is real, so record it, but the order is
            # already paid: don't rewrite state or re-notify. (#77)
            _record_payment_event(db, order, _parse_float(data.get("amount")))
            return {"status": "success", "message": "Order already PAID; payment recorded"}

        if order.status in (OrderStatus.FAILED, OrderStatus.CANCELLED):
            # Customer paid after the order resolved without payment — and
            # both FAILED and CANCELLED restore the stock reservation
            # (expiry via auto_cancel_order, or a failed Pay Now dispatch).
            # Re-claim before marking paid, or the sale completes without
            # decrementing. (#77, #110)
            if not _try_reclaim_stock(db, order):
                if transaction_id:
                    order.tunzaa_transaction_id = transaction_id
                db.commit()
                logger.error(
                    f"COMPLETED for order {order.id} but no stock to reclaim; "
                    f"leaving {order.status.value} for merchant review."
                )
                return {"status": "success", "message": "Payment arrived but stock unavailable"}
            order.status = OrderStatus.PENDING

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
        _record_payment_event(db, order, _parse_float(data.get("amount")))

        # Stock was already decremented at reservation time (order creation
        # in app/services/llm.py) — nothing further to do there. Just
        # confirm to the customer.
        await _notify_customer(db, order, "payment_complete")
        await _activate_subscription_if_linked(db, order)
        await _notify_merchant_of_payment(db, order)

        return {"status": "success", "message": f"Order status: {order.status.value}"}

    elif status_value in ("FAILED", "CANCELLED"):
        if order.status != OrderStatus.PENDING:
            # A FAILED for an already-resolved order (e.g. an abandoned
            # duplicate attempt after the real one paid) must never regress
            # it — or restore stock twice. (#77)
            return {"status": "ignored", "reason": "Order already resolved"}
        order.status = OrderStatus.FAILED
        if transaction_id:
            order.tunzaa_transaction_id = transaction_id
        restored_product_id = _release_reservation(db, order)
        db.commit()
        if restored_product_id:
            # The release may flip the catalog's availability back to in stock.
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(restored_product_id, "upsert")

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


def _record_payment_event(db: Session, order: Order, amount: Optional[float]) -> None:
    """
    TZS collected, for GET /stats (#41). Runs after both idempotency guards
    above, so a Tunzaa retry can't double-count. Falls back to the agreed
    price only for a one-off full payment, where that's the whole amount.
    """
    merchant = _merchant_for_order(order)
    if amount is None and not order.tunzaa_plan_id:
        amount = order.agreed_price
    usage.record_event(
        db, merchant.id if merchant else None, AmaraEventKind.PAYMENT,
        channel="whatsapp" if order.account_id else "instagram_dm",
        account_id=order.account_id, instagram_account_id=order.instagram_account_id,
        product_name=order.product.name if order.product else None, amount=amount,
    )


def _merchant_for_order(order: Order):
    if order.account_id:
        return order.account.owner if order.account else None
    if order.instagram_account_id:
        return order.instagram_account.owner if order.instagram_account else None
    return None


def _payment_alert_summary_and_reference(order: Order) -> tuple[str, str]:
    """
    Builds the {{summary}} and {{reference}} values for the payment_received
    template — one variable per template line, each wrapped by that line's
    own static text, since a newline embedded inside a single variable's
    value doesn't render as a line break (found live while designing this).
    """
    amount = f"{order.agreed_price:,.0f} TZS" if order.agreed_price is not None else "an unknown amount"

    # Keyed off tunzaa_plan_id (same signal the main handler above uses to
    # decide whether to reconcile against the plan) rather than
    # payment_type — this is what actually determines whether this
    # callback is one leg of a tracked multi-installment plan.
    is_installment_plan = bool(order.tunzaa_plan_id)
    if is_installment_plan:
        if order.status == OrderStatus.PAID:
            payment_desc = "final installment — fully paid"
        else:
            payment_desc = "installment payment"
        if order.paid_amount is not None and order.remaining_balance is not None:
            status_text = (
                f"Total paid: {order.paid_amount:,.0f} TZS. "
                f"Remaining: {order.remaining_balance:,.0f} TZS."
            )
        else:
            status_text = "Paid in full." if order.status == OrderStatus.PAID else "Partial payment received."
    else:
        payment_desc = "installment payment" if order.payment_type == PaymentType.INSTALLMENT else "full payment"
        status_text = "Paid in full."

    summary = f"{amount} ({payment_desc})"

    if order.account_id and order.customer:
        customer_ref, channel = order.customer.phone_number, "WhatsApp"
    elif order.instagram_account_id and order.instagram_customer:
        handle = order.instagram_customer.username or order.instagram_customer.ig_scoped_id
        customer_ref, channel = f"@{handle}", "Instagram"
    elif order.customer:
        customer_ref, channel = order.customer.phone_number, "an unknown channel"
    else:
        customer_ref, channel = "an unknown customer", "an unknown channel"

    txn = order.tunzaa_transaction_id or "unknown"
    reference = f"From {customer_ref} via {channel} · {status_text} · Txn {txn}"

    return summary, reference


async def _notify_merchant_of_payment(db: Session, order: Order) -> None:
    """
    Alerts the merchant of any successful payment — full, an installment
    leg (including a mid-plan one), or whatever activates/completes a
    subscription — via the platform's own default WhatsApp credentials, the
    same channel OTP and the handoff alert use. Not the merchant's own
    connected account: a merchant essentially never has an open 24h session
    with either number, so this needs the approved payment_received
    template regardless of which number sends it. Best-effort/silent if the
    merchant has no phone on file. See #35.
    """
    merchant = _merchant_for_order(order)
    merchant_phone = (merchant.username or merchant.phone_number) if merchant else None
    if not merchant_phone:
        logger.info(f"No merchant phone on file for order {order.id}; skipping payment alert.")
        return

    product_name = order.product.name if order.product else "your order"
    summary, reference = _payment_alert_summary_and_reference(order)

    components = [
        {
            "type": "header",
            "parameters": [{"type": "text", "parameter_name": "product", "text": product_name}],
        },
        {
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": "summary", "text": summary},
                {"type": "text", "parameter_name": "reference", "text": reference},
            ],
        },
    ]

    provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
    try:
        await provider.send_template(
            merchant_phone,
            settings.WHATSAPP_PAYMENT_ALERT_TEMPLATE_NAME,
            language_code=settings.WHATSAPP_PAYMENT_ALERT_TEMPLATE_LANGUAGE,
            components=components,
        )
    except Exception as e:
        logger.error(f"Failed to send merchant payment alert for order {order.id}: {e}")


def _mark_single_installment_paid(db: Session, order: Order, installment_id) -> None:
    installment = db.query(Installment).filter(
        Installment.order_id == order.id,
        Installment.tunzaa_installment_id == installment_id,
    ).first()
    if installment:
        installment.status = InstallmentStatus.PAID


def apply_installment_plan_state(db: Session, order: Order, plan_data: dict) -> None:
    """
    Syncs local Installment/Order rows from a Tunzaa plan fetch — plan-level
    paid_amount/remaining_balance plus each installment's own status. Shared
    by the webhook reconcile below and the background retry task (which
    imports this; see app/tasks/product_subscription_billing.py). Mutates
    without committing — the caller commits.
    """
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


async def _reconcile_installment_plan(db: Session, order: Order, installment_id) -> None:
    """
    Fetches the plan's authoritative state from Tunzaa (paid_amount/
    remaining_balance at the plan level, plus each installment's own
    status) and syncs our local rows from it, rather than trusting this
    one callback's fields in isolation. If the Tunzaa call itself fails,
    only the paid leg is recorded and the order stays PENDING while a
    background task retries the fetch — the order-level verdict must come
    from Tunzaa's balances, never from a failed lookup (#110).
    """
    from app.services.tunzaa import tunzaa_service

    try:
        plan_data = await tunzaa_service.get_installment_plan(
            order.tunzaa_plan_id, include_payments=True
        )
    except Exception as e:
        # The callback is signed proof this leg was paid, so record that —
        # but without the plan's authoritative balances the whole order
        # must not be marked paid (a mid-plan leg plus a transient fetch
        # error used to mark the entire order PAID). Settle it from a
        # background retry instead.
        logger.warning(
            f"get_installment_plan failed for order {order.id} "
            f"(plan {order.tunzaa_plan_id}); marking the leg and retrying: {e}"
        )
        _mark_single_installment_paid(db, order, installment_id)
        try:
            from app.tasks.product_subscription_billing import (
                RECONCILE_RETRY_DELAY_SECONDS,
                retry_installment_reconcile,
            )
            retry_installment_reconcile.apply_async(
                args=[str(order.id), 1],
                countdown=RECONCILE_RETRY_DELAY_SECONDS,
            )
        except Exception as sched_err:
            logger.error(
                f"Failed to schedule reconcile retry for order {order.id}: {sched_err}"
            )
        return

    apply_installment_plan_state(db, order, plan_data)


def _parse_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _release_reservation(db: Session, order: Order) -> Optional[uuid.UUID]:
    """Restores stock reserved at order-creation time for a failed/cancelled order.

    Returns the restored product's id, or None when nothing was restored.
    """
    if not order.product_id:
        return None
    product = db.query(Product).filter(Product.id == order.product_id).first()
    if product:
        product.quantity += order.quantity or 1
        return product.id
    return None


def _try_reclaim_stock(db: Session, order: Order) -> bool:
    """
    Re-decrements stock for a COMPLETED that arrives after FAILED restored
    the reservation (#77). Conditional UPDATE like _try_claim_stock: the
    rowcount decides, so two racing callbacks can't both claim the last
    unit. True when stock was (re)taken or there's nothing to track.
    """
    if not order.product_id:
        return True
    product = db.query(Product).filter(Product.id == order.product_id).first()
    if product is None:
        return True
    units = order.quantity or 1
    claimed = (
        db.query(Product)
        .filter(Product.id == order.product_id, Product.quantity >= units)
        .update(
            {Product.quantity: Product.quantity - units},
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed > 0


_PAYMENT_COMPLETE_LINES = {
    "sw": "🎉 Malipo yamethibitishwa! Asante kwa oda yako.",
    "en": "🎉 Payment confirmed! Thank you for your order.",
}

_PAYMENT_COMPLETE_DELIVERY = {
    "sw": "Tutawasiliana nawe hivi punde kuhusu kupelekewa.",
    "en": "We'll be in touch shortly about delivery.",
}

_PAYMENT_COMPLETE_SUBSCRIPTION = {
    "sw": "Usajili wako umeanza — karibu!",
    "en": "Your subscription is now active — welcome!",
}

_PAYMENT_FAILED_LINES = {
    "sw": (
        "Malipo yako hayakukamilika, kwa hivyo oda hii imefutwa. "
        "Tuma ujumbe tena ukitaka kujaribu tena."
    ),
    "en": (
        "Your payment could not be completed, so this order has been "
        "cancelled and the item released. Message us again if you'd "
        "like to try again."
    ),
}


def _payment_notification_body(kind: str, order: Order) -> str:
    language = order.customer.language if order.customer else None
    if kind == "payment_complete":
        body = _lang_text(_PAYMENT_COMPLETE_LINES, language)
        # Subscriptions have nothing to deliver — and this confirmation
        # is the one place that may truthfully say the subscription is
        # active, since Tunzaa just confirmed it (#104).
        if order.product is not None and order.product.is_subscription:
            tail = _lang_text(_PAYMENT_COMPLETE_SUBSCRIPTION, language)
        else:
            tail = _lang_text(_PAYMENT_COMPLETE_DELIVERY, language)
        if language in ("sw", "en"):
            return f"{body} {tail}"
        # Unknown language: both halves are sw/en stacks — join per
        # language to keep the message readable.
        sw_body, en_body = body.split("\n\n")
        sw_tail, en_tail = tail.split("\n\n")
        return f"{sw_body} {sw_tail}\n\n{en_body} {en_tail}"
    return _lang_text(_PAYMENT_FAILED_LINES, language)


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

    body = _payment_notification_body(kind, order)

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(order.customer.phone_number, body)
    except Exception as e:
        logger.error(f"Failed to send Tunzaa payment notification to customer: {e}")
        resp = None
    if usage.whatsapp_send_succeeded(resp):
        usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)

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

    body = _payment_notification_body(kind, order)

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
    usage.record_reply(db, account.user_id, "instagram_dm", instagram_account_id=account.id)
