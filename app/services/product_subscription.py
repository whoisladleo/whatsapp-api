import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Order, Product, ProductSubscription, ProductSubscriptionStatus, User
from app.services import usage

logger = logging.getLogger(__name__)

RENEWAL_PERIOD_DAYS = 30
# 10 attempts, 36h apart — ~15 days, the closest clean interval to the
# "10 retries over 2 weeks" policy (14 days / 10 is an ugly 33.6h).
RENEWAL_RETRY_DELAY_SECONDS = 36 * 60 * 60
MAX_RENEWAL_RETRIES = 10
# A renewal callback is only treated as a stale duplicate while the paid
# cycle is still more than this far from due — the 1h absorbs
# scheduler/clock skew so a genuine next-cycle signal (which always arrives
# at/after next_billing_at) is never mistaken for one.
RENEWAL_DUE_GRACE_SECONDS = 3600


def get_or_create_pending_subscription(
    db: Session,
    order: Order,
    product: Product,
    customer_id: uuid.UUID,
    instagram_customer_id: Optional[uuid.UUID] = None,
) -> ProductSubscription:
    """
    Called wherever a subscription-product deal closes (LLMService._create_subscription_if_needed,
    self_service_billing.pay_now) — reuses an existing PENDING_PAYMENT
    subscription for this exact customer+product instead of inserting a new
    row every time. Without this, a customer who retries before their
    first payment ever confirms (abandoned Tunzaa prompt, repeated "Pay
    Now" clicks, re-negotiating in a later chat) accumulates one orphaned
    PENDING_PAYMENT row per attempt — none of which ever resolve, since
    only the newest one is ever checked (find_platform_subscription orders
    by created_at desc) or paid against, and all of them skew any
    reporting/counting done against this table. Re-pointing the existing
    row at the new Order is correct here: the old order is abandoned (never
    paid), so nothing else still depends on that order_id.
    """
    existing = (
        db.query(ProductSubscription)
        .filter(
            ProductSubscription.customer_id == customer_id,
            ProductSubscription.product_id == product.id,
            ProductSubscription.status == ProductSubscriptionStatus.PENDING_PAYMENT,
        )
        .first()
    )
    if existing:
        existing.order_id = order.id
        existing.monthly_amount = order.agreed_price
        if instagram_customer_id:
            existing.instagram_customer_id = instagram_customer_id
        db.commit()
        return existing

    subscription = ProductSubscription(
        order_id=order.id,
        customer_id=customer_id,
        product_id=product.id,
        instagram_customer_id=instagram_customer_id,
        monthly_amount=order.agreed_price,
    )
    db.add(subscription)
    db.commit()
    return subscription


def _resolve_merchant_for_platform_subscription(db: Session, subscription: ProductSubscription) -> Optional[User]:
    """
    A ProductSubscription can belong to *any* merchant's subscription
    product — most only need the billing mechanics above. Access gating
    (app/services/subscription_access.py) only applies to platform plan
    SKUs (Product.is_platform_plan — a merchant paying *Taskflow* for the
    platform itself), so this resolves "which Taskflow dashboard User does
    this subscription's paying customer correspond to" — and returns None
    for every other merchant's subscription products, which must never
    affect anyone's ai_enabled.
    """
    product = subscription.product or db.query(Product).filter(Product.id == subscription.product_id).first()
    if not product or not product.is_platform_plan:
        return None

    customer = subscription.customer
    phone = customer.phone_number if customer else None
    if not phone:
        return None

    return (
        db.query(User)
        .filter((User.username == phone) | (User.phone_number == phone))
        .first()
    )


def sync_access_if_platform_subscription(db: Session, subscription: ProductSubscription) -> None:
    from app.services import subscription_access

    merchant = _resolve_merchant_for_platform_subscription(db, subscription)
    if merchant:
        subscription_access.sync_ai_access_for_merchant(db, merchant)


def activate_after_first_payment(db: Session, subscription: ProductSubscription) -> None:
    """
    Called from the Tunzaa webhook's normal Order-COMPLETED handling
    (app/api/v1/tunzaa_webhook.py) once the first charge for a
    subscription-flagged product's order is confirmed. Flips the
    subscription live and schedules its first renewal.
    """
    from app.tasks.product_subscription_billing import charge_renewal

    subscription.status = ProductSubscriptionStatus.ACTIVE
    subscription.retry_count = 0
    subscription.past_due_since = None
    subscription.next_billing_at = datetime.utcnow() + timedelta(days=RENEWAL_PERIOD_DAYS)
    db.commit()

    try:
        charge_renewal.apply_async(
            args=[str(subscription.id)], countdown=RENEWAL_PERIOD_DAYS * 86400
        )
    except Exception as e:
        logger.error(f"Failed to schedule first renewal for subscription {subscription.id}: {e}")

    sync_access_if_platform_subscription(db, subscription)


def mark_first_payment_failed(db: Session, subscription: ProductSubscription) -> None:
    """Called from the same webhook's FAILED/CANCELLED branch. No retry —
    matches how a normal (non-subscription) Order failure already works;
    the customer just has to ask Amara again."""
    subscription.status = ProductSubscriptionStatus.PAYMENT_FAILED
    db.commit()

    sync_access_if_platform_subscription(db, subscription)


def _record_renewal_payment(db: Session, subscription: ProductSubscription) -> None:
    """TZS collected from a renewal charge, for GET /stats (#41)."""
    from app.db.models import AmaraEventKind

    order = subscription.order
    account = (order.account or order.instagram_account) if order else None
    owner = account.owner if account else None
    usage.record_event(
        db, owner.id if owner else None, AmaraEventKind.PAYMENT,
        channel="whatsapp" if order and order.account_id else "instagram_dm",
        account_id=order.account_id if order else None,
        instagram_account_id=order.instagram_account_id if order else None,
        product_name=order.product.name if order and order.product else None,
        amount=subscription.monthly_amount,
    )


def _current_cycle_already_paid(subscription: ProductSubscription) -> bool:
    """True when the current cycle was already paid for and isn't due yet —
    ACTIVE with retry_count reset and next_billing_at still in the future.
    A renewal callback arriving in that state is a stale duplicate for the
    paid cycle, not a genuine signal about an outstanding charge. Anything
    else — PAST_DUE, a missing date, or a due/overdue date — must be
    processed normally."""
    if subscription.status != ProductSubscriptionStatus.ACTIVE:
        return False
    if subscription.retry_count != 0:
        return False
    if not subscription.next_billing_at:
        return False
    return subscription.next_billing_at > datetime.utcnow() + timedelta(
        seconds=RENEWAL_DUE_GRACE_SECONDS
    )


async def handle_renewal_payment_result(
    db: Session, subscription: ProductSubscription, status_value: Optional[str], transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "product-subscription-renewal-<id>" reference."""
    from app.tasks.product_subscription_billing import charge_renewal, check_past_due_grace_expired

    if status_value == "COMPLETED":
        if _current_cycle_already_paid(subscription):
            # P4: duplicate COMPLETED for the already-paid cycle — the
            # renewal task is already outstanding, so don't move
            # next_billing_at or schedule a second one (two tasks would each
            # push next cycle). Whether Tunzaa dedupes same-reference pushes
            # is unverified, so single-task scheduling is enforced here
            # rather than assumed there.
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
        subscription.status = ProductSubscriptionStatus.ACTIVE
        subscription.retry_count = 0
        subscription.past_due_since = None
        subscription.next_billing_at = datetime.utcnow() + timedelta(days=RENEWAL_PERIOD_DAYS)
        db.commit()
        _record_renewal_payment(db, subscription)
        try:
            charge_renewal.apply_async(
                args=[str(subscription.id)], countdown=RENEWAL_PERIOD_DAYS * 86400
            )
        except Exception as e:
            logger.error(f"Failed to schedule next renewal for subscription {subscription.id}: {e}")

        sync_access_if_platform_subscription(db, subscription)

    elif status_value in ("FAILED", "CANCELLED"):
        if _current_cycle_already_paid(subscription):
            # P3: stale FAILED for the already-paid cycle — must not regress
            # to PAST_DUE, schedule a retry, or start the past-due grace
            # clock (that double-charges at +36h and +30d). Genuine
            # next-cycle failures arrive once next_billing_at is due, so
            # they never hit this path.
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
        if subscription.retry_count < MAX_RENEWAL_RETRIES:
            subscription.status = ProductSubscriptionStatus.PAST_DUE
            subscription.retry_count += 1
            # Only the *first* failure in a past-due streak starts the
            # access-grace clock and schedules its expiry check — later
            # retries within the same streak leave it alone.
            newly_past_due = subscription.past_due_since is None
            if newly_past_due:
                subscription.past_due_since = datetime.utcnow()
            db.commit()
            try:
                charge_renewal.apply_async(
                    args=[str(subscription.id)], countdown=RENEWAL_RETRY_DELAY_SECONDS
                )
            except Exception as e:
                logger.error(f"Failed to schedule renewal retry for subscription {subscription.id}: {e}")

            if newly_past_due:
                from app.services.subscription_access import PAST_DUE_ACCESS_GRACE_DAYS

                try:
                    check_past_due_grace_expired.apply_async(
                        args=[str(subscription.id)], countdown=PAST_DUE_ACCESS_GRACE_DAYS * 86400
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to schedule past-due grace check for subscription {subscription.id}: {e}"
                    )

            await notify_customer(
                db, subscription,
                f"Your payment for {subscription.product.name} ({int(subscription.monthly_amount):,} TZS) "
                "didn't go through. We'll try again soon — please make sure you can approve the "
                "mobile money prompt.",
            )
            await notify_merchant(
                db, subscription,
                f"⚠️ A renewal payment for {subscription.product.name} failed "
                f"(attempt {subscription.retry_count}/{MAX_RENEWAL_RETRIES}). Retrying automatically.",
            )
            sync_access_if_platform_subscription(db, subscription)
        else:
            subscription.status = ProductSubscriptionStatus.CANCELLED
            db.commit()

            await notify_customer(
                db, subscription,
                f"Your subscription to {subscription.product.name} has been cancelled after "
                "repeated failed payments. Message us again if you'd like to restart it.",
            )
            await notify_merchant(
                db, subscription,
                f"🚨 Subscription to {subscription.product.name} was cancelled after "
                f"{MAX_RENEWAL_RETRIES} failed renewal attempts.",
            )
            sync_access_if_platform_subscription(db, subscription)
    else:
        # Unknown/missing status: must NOT record the txn id — the
        # webhook's same-txn guard would then discard the genuine
        # COMPLETED with this id as "Already processed", leaving a paid
        # cycle never activated. Mirrors the Order path, which stores the
        # txn id only inside the COMPLETED/FAILED branches.
        db.commit()


async def notify_customer(db: Session, subscription: ProductSubscription, body: str) -> None:
    """
    Enforces a single channel per subscription rather than broadcasting —
    whichever one the deal actually closed on. A WhatsApp-originated
    subscription's customer only ever talked to the shop over WhatsApp, and
    a business-initiated WhatsApp message outside an open session generally
    needs a pre-approved template anyway, so "also try WhatsApp" for an
    Instagram-only relationship wouldn't reliably deliver even if attempted.
    """
    if subscription.instagram_customer_id:
        await _notify_customer_instagram(db, subscription, body)
    elif subscription.order and subscription.order.account_id:
        await _notify_customer_whatsapp(db, subscription, body)
    # else: no channel on file for this subscription — nothing to notify.


async def _notify_customer_whatsapp(db: Session, subscription: ProductSubscription, body: str) -> None:
    from app.core.provider import WhatsAppProvider
    from app.crud import message as crud_message

    account = subscription.order.account
    customer = subscription.customer
    if not account or not customer:
        return

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(customer.phone_number, body)
    except Exception as e:
        logger.error(f"Failed to send WhatsApp renewal notice for subscription {subscription.id}: {e}")
        resp = None
    if usage.whatsapp_send_succeeded(resp):
        usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)

    crud_message.create_message(
        db,
        {
            "to_phone": customer.phone_number,
            "type": "text",
            "body": body,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )


async def _notify_customer_instagram(db: Session, subscription: ProductSubscription, body: str) -> None:
    from app.core.instagram_provider import InstagramProvider
    from app.crud import instagram as crud_instagram

    ig_customer = subscription.instagram_customer
    account = subscription.order.instagram_account if subscription.order else None
    if not ig_customer or not account:
        return

    provider = InstagramProvider(account.access_token)
    try:
        resp = await provider.send_direct_message(account.ig_business_id, ig_customer.ig_scoped_id, body)
    except Exception as e:
        logger.error(f"Failed to send Instagram renewal DM for subscription {subscription.id}: {e}")
        resp = {}

    if "error" in resp:
        logger.error(f"Instagram renewal DM failed for subscription {subscription.id}: {resp}")
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


async def notify_merchant(db: Session, subscription: ProductSubscription, body: str) -> None:
    """
    Best-effort WhatsApp push to the merchant, on top of whatever surfaces
    in the dashboard (Order.subscription_status/next_billing_at — see
    crud/order.py). Only reaches merchants with a WhatsAppAccount; an
    Instagram-only merchant gets no push alert, just the dashboard data —
    there's no equivalent "message the merchant" channel over Instagram
    (they're the account operator, not a DM peer of their own account).
    """
    from app.core.provider import WhatsAppProvider

    merchant = subscription.product.merchant if subscription.product else None
    if not merchant or not merchant.username:
        return

    account = merchant.accounts[0] if merchant.accounts else None
    if not account:
        return

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        await provider.send_text(merchant.username, body)
    except Exception as e:
        logger.error(f"Failed to send merchant alert for subscription {subscription.id}: {e}")
