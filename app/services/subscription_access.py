import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import (
    Customer,
    InstagramAccount,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)

# See ProductSubscription.past_due_since — a subscription that's been
# PAST_DUE this long (still short of the 10-retry/~15-day cancellation
# point) already gets Amara paused, ahead of full cancellation.
PAST_DUE_ACCESS_GRACE_DAYS = 7


def get_platform_subscription_product(db: Session) -> Optional[Product]:
    if not settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG:
        return None
    return (
        db.query(Product)
        .filter(Product.slug == settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG)
        .first()
    )


def find_platform_subscription(db: Session, user: User) -> Optional[ProductSubscription]:
    product = get_platform_subscription_product(db)
    if not product:
        return None

    candidates = {
        p for p in (normalize_phone(user.username), normalize_phone(user.phone_number)) if p
    }
    if not candidates:
        return None

    customer = db.query(Customer).filter(Customer.phone_number.in_(candidates)).first()
    if not customer:
        return None

    return (
        db.query(ProductSubscription)
        .filter(ProductSubscription.customer_id == customer.id, ProductSubscription.product_id == product.id)
        .order_by(ProductSubscription.created_at.desc())
        .first()
    )


def is_merchant_allowed_ai(db: Session, user: User) -> bool:
    """
    Whether this merchant's Amara should be active anywhere. Gating is a
    no-op (everyone allowed) while PLATFORM_SUBSCRIPTION_PRODUCT_SLUG isn't
    configured, or if that product can't be found (fail open — a
    misconfigured slug shouldn't silently lock out every merchant). With it
    configured: no subscription on file at all, a CANCELLED one, or a
    first-payment PAYMENT_FAILED all deny access, the same as each other —
    "never paid" and "stopped paying" are treated identically. A PAST_DUE
    subscription still gets a grace period (see PAST_DUE_ACCESS_GRACE_DAYS)
    before it denies too.
    """
    if not settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG:
        return True

    product = get_platform_subscription_product(db)
    if not product:
        logger.error(
            "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG is set but no matching Product "
            "exists; failing open (not gating anyone) rather than locking out "
            "every merchant over a config error."
        )
        return True

    # The merchant who *sells* the platform subscription product can't also
    # be gated by it — there's no one for Taskflow's own account(s) to
    # subscribe to, and this is exactly the account that most needs to keep
    # responding (it's the one pitching new merchants). Exempts every
    # account this merchant owns, the same way llm.py's is_platform_account
    # check exempts the WhatsApp "taskflow" account from being negotiated
    # with as a customer.
    if product.merchant_id == user.id:
        return True

    subscription = find_platform_subscription(db, user)
    if not subscription:
        return False

    if subscription.status in (
        ProductSubscriptionStatus.CANCELLED,
        ProductSubscriptionStatus.PAYMENT_FAILED,
    ):
        return False

    if subscription.status == ProductSubscriptionStatus.PAST_DUE:
        if subscription.past_due_since is None:
            return True
        return (datetime.utcnow() - subscription.past_due_since) < timedelta(days=PAST_DUE_ACCESS_GRACE_DAYS)

    # ACTIVE, or PENDING_PAYMENT (first payment still in flight) — allowed.
    return True


def sync_ai_access_for_merchant(db: Session, user: User) -> None:
    """
    Applies the current access decision to every account this merchant
    owns. This is the sole place ai_enabled gets set for WhatsApp/Instagram
    accounts belonging to a gated merchant — called after every subscription
    status change (app/services/product_subscription.py) and every account
    create/update/reconnect (app/crud/whatsapp.py, app/crud/instagram.py),
    so it always overrides whatever ai_enabled value a request tried to set.
    Deliberately skips a disconnected Instagram account (access_token ==
    "") — re-enabling it here would look like a reconnect it never had.
    """
    allowed = is_merchant_allowed_ai(db, user)

    changed = False
    for account in db.query(WhatsAppAccount).filter(WhatsAppAccount.user_id == user.id).all():
        if account.ai_enabled != allowed:
            account.ai_enabled = allowed
            changed = True

    for account in (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == user.id, InstagramAccount.access_token != "")
        .all()
    ):
        if account.ai_enabled != allowed:
            account.ai_enabled = allowed
            changed = True

    if changed:
        db.commit()
