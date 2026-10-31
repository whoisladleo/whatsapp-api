from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

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

# See ProductSubscription.past_due_since — a subscription that's been
# PAST_DUE this long (still short of the 10-retry/~15-day cancellation
# point) already gets Amara paused, ahead of full cancellation.
PAST_DUE_ACCESS_GRACE_DAYS = 7


def get_platform_plans(db: Session) -> list[Product]:
    """Every Product flagged as a platform plan SKU (#90), oldest first."""
    return (
        db.query(Product)
        .filter(Product.is_platform_plan.is_(True))
        .order_by(Product.created_at)
        .all()
    )


def get_platform_subscription_product(db: Session) -> Optional[Product]:
    """First configured plan — compat default for single-plan flows."""
    plans = get_platform_plans(db)
    return plans[0] if plans else None


def find_platform_subscriptions(db: Session, user: User) -> list[ProductSubscription]:
    """Every platform-plan subscription for this merchant's paying identity,
    across all plan SKUs, newest first."""
    plans = get_platform_plans(db)
    if not plans:
        return []

    candidates = {
        p for p in (normalize_phone(user.username), normalize_phone(user.phone_number)) if p
    }
    if not candidates:
        return []

    customer = db.query(Customer).filter(Customer.phone_number.in_(candidates)).first()
    if not customer:
        return []

    return (
        db.query(ProductSubscription)
        .filter(
            ProductSubscription.customer_id == customer.id,
            ProductSubscription.product_id.in_([p.id for p in plans]),
        )
        .order_by(ProductSubscription.created_at.desc())
        .all()
    )


def find_platform_subscription(db: Session, user: User) -> Optional[ProductSubscription]:
    """Newest platform-plan subscription — compat shim for single-sub flows."""
    subs = find_platform_subscriptions(db, user)
    return subs[0] if subs else None


def _subscription_allows(subscription: ProductSubscription) -> bool:
    """One subscription's access verdict. CANCELLED or first-payment
    PAYMENT_FAILED deny, the same as each other — "never paid" and
    "stopped paying" are treated identically. A PAST_DUE subscription
    still gets a grace period (see PAST_DUE_ACCESS_GRACE_DAYS) before it
    denies too. ACTIVE, or PENDING_PAYMENT (first payment still in
    flight), allow."""
    if subscription.status in (
        ProductSubscriptionStatus.CANCELLED,
        ProductSubscriptionStatus.PAYMENT_FAILED,
    ):
        return False

    if subscription.status == ProductSubscriptionStatus.PAST_DUE:
        if subscription.past_due_since is None:
            return True
        return (datetime.utcnow() - subscription.past_due_since) < timedelta(days=PAST_DUE_ACCESS_GRACE_DAYS)

    return True


def is_merchant_allowed_ai(db: Session, user: User) -> bool:
    """
    Whether this merchant's Amara should be active anywhere. Gating is a
    no-op (everyone allowed) while no platform plan SKU exists — fail
    open, the same as the old unset-slug behavior. With plans
    configured: any one allowing subscription (across all plan SKUs)
    grants access.
    """
    plans = get_platform_plans(db)
    if not plans:
        return True

    # The merchant who *sells* a platform plan can't also be gated by it —
    # there's no one for Taskflow's own account(s) to subscribe to, and
    # this is exactly the account that most needs to keep responding
    # (it's the one pitching new merchants). Exempts every account this
    # merchant owns, the same way llm.py's is_platform_account check
    # exempts the WhatsApp "taskflow" account from being negotiated with
    # as a customer.
    if any(plan.merchant_id == user.id for plan in plans):
        return True

    return any(
        _subscription_allows(sub)
        for sub in find_platform_subscriptions(db, user)
    )


def plan_reply_limit(db: Session, user: User) -> Optional[int]:
    """
    This merchant's monthly Amara-reply meter (#90): the highest
    monthly_reply_limit across their access-granting plan subscriptions.
    None means unlimited — no plans configured, seller-exempt, or at
    least one granting plan with no limit set.
    """
    plans = get_platform_plans(db)
    if not plans:
        return None
    if any(plan.merchant_id == user.id for plan in plans):
        return None
    limits = [
        (sub.product.monthly_reply_limit if sub.product else None)
        for sub in find_platform_subscriptions(db, user)
        if _subscription_allows(sub)
    ]
    if not limits or any(limit is None for limit in limits):
        return None
    return max(limits)


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
