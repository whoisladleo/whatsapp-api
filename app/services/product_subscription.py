import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Product, ProductSubscription, ProductSubscriptionStatus, User

logger = logging.getLogger(__name__)

RENEWAL_PERIOD_DAYS = 30
# 10 attempts, 36h apart — ~15 days, the closest clean interval to the
# "10 retries over 2 weeks" policy (14 days / 10 is an ugly 33.6h).
RENEWAL_RETRY_DELAY_SECONDS = 36 * 60 * 60
MAX_RENEWAL_RETRIES = 10


def _resolve_merchant_for_platform_subscription(db: Session, subscription: ProductSubscription) -> Optional[User]:
    """
    A ProductSubscription can belong to *any* merchant's subscription
    product — most only need the billing mechanics above. Access gating
    (app/services/subscription_access.py) only applies to the one specific
    product identified by settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG (a
    merchant paying *Taskflow* for the platform itself), so this resolves
    "which Taskflow dashboard User does this subscription's paying customer
    correspond to" — and returns None for every other merchant's
    subscription products, which must never affect anyone's ai_enabled.
    """
    from app.core.config import settings

    if not settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG:
        return None

    product = subscription.product or db.query(Product).filter(Product.id == subscription.product_id).first()
    if not product or product.slug != settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG:
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
            args=[subscription.id], countdown=RENEWAL_PERIOD_DAYS * 86400
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


async def handle_renewal_payment_result(
    db: Session, subscription: ProductSubscription, status_value: Optional[str], transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "product-subscription-renewal-<id>" reference."""
    from app.tasks.product_subscription_billing import charge_renewal, check_past_due_grace_expired

    if transaction_id:
        subscription.tunzaa_transaction_id = transaction_id

    if status_value == "COMPLETED":
        subscription.status = ProductSubscriptionStatus.ACTIVE
        subscription.retry_count = 0
        subscription.past_due_since = None
        subscription.next_billing_at = datetime.utcnow() + timedelta(days=RENEWAL_PERIOD_DAYS)
        db.commit()
        try:
            charge_renewal.apply_async(
                args=[subscription.id], countdown=RENEWAL_PERIOD_DAYS * 86400
            )
        except Exception as e:
            logger.error(f"Failed to schedule next renewal for subscription {subscription.id}: {e}")

        sync_access_if_platform_subscription(db, subscription)

    elif status_value in ("FAILED", "CANCELLED"):
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
                    args=[subscription.id], countdown=RENEWAL_RETRY_DELAY_SECONDS
                )
            except Exception as e:
                logger.error(f"Failed to schedule renewal retry for subscription {subscription.id}: {e}")

            if newly_past_due:
                from app.services.subscription_access import PAST_DUE_ACCESS_GRACE_DAYS

                try:
                    check_past_due_grace_expired.apply_async(
                        args=[subscription.id], countdown=PAST_DUE_ACCESS_GRACE_DAYS * 86400
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
