"""
Amara's usage ledger (#41): records billable sends, quotes, handoffs and
payments as AmaraEvent rows, and works out each merchant's billing period
and reply count against settings.PLAN_REPLY_LIMIT.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AmaraEvent, AmaraEventKind, ProductSubscriptionStatus, User

logger = logging.getLogger(__name__)

WARNING_THRESHOLDS_PERCENT = (80, 100)
EAT_OFFSET = timedelta(hours=3)  # East Africa Time, no DST

# Sent to the customer instead of an Amara reply once the merchant's
# meter is exhausted (#90). Deliberately not metered itself — recording
# it as a REPLY would inflate the count it's reporting.
QUOTA_EXHAUSTED_HOLDING_MESSAGE = (
    "This shop has used all of its Amara replies for this month, so I "
    "can't respond right now — the owner has been notified."
)


def whatsapp_send_succeeded(resp) -> bool:
    return bool(resp) and "messages" in resp


def instagram_send_succeeded(resp) -> bool:
    return bool(resp) and "error" not in resp


def record_event(
    db: Session,
    merchant_id,
    kind: AmaraEventKind,
    channel: Optional[str] = None,
    account_id=None,
    instagram_account_id=None,
    product_name: Optional[str] = None,
    amount: Optional[float] = None,
    detail: Optional[str] = None,
) -> None:
    """
    Never raises: the ledger is written after the real side effect (a
    message already sent, a payment already confirmed), so a failure here
    must not turn into a retry that repeats that side effect. It's logged
    loudly instead, since a missing REPLY row is under-billing.
    """
    if merchant_id is None:
        return
    try:
        db.add(AmaraEvent(
            merchant_id=merchant_id, kind=kind, channel=channel, account_id=account_id,
            instagram_account_id=instagram_account_id, product_name=product_name,
            amount=amount, detail=detail,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(f"Failed to record {kind.value} event for merchant {merchant_id}")


def record_reply(db: Session, merchant_id, channel: str, account_id=None, instagram_account_id=None) -> None:
    """Records one billable send, then fires any usage warning it just crossed."""
    record_event(
        db, merchant_id, AmaraEventKind.REPLY, channel=channel,
        account_id=account_id, instagram_account_id=instagram_account_id,
    )
    try:
        _warn_if_threshold_crossed(db, merchant_id)
    except Exception:
        db.rollback()
        logger.exception(f"Usage warning check failed for merchant {merchant_id}")


def billing_period(db: Session, merchant: User, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """
    [start, end) in naive UTC. A merchant with a live Taskflow subscription
    is metered on its own billing cycle; anyone else (founding / manually
    billed shops) on the calendar month in EAT.
    """
    from app.services.product_subscription import RENEWAL_PERIOD_DAYS
    from app.services.subscription_access import find_platform_subscriptions

    now = now or datetime.utcnow()
    # Oldest live plan anchors the meter — upgrading mid-period must not
    # reset the window (find_platform_subscriptions returns newest first).
    subscriptions = [
        sub
        for sub in reversed(find_platform_subscriptions(db, merchant))
        if sub.next_billing_at
        and sub.status in (ProductSubscriptionStatus.ACTIVE, ProductSubscriptionStatus.PAST_DUE)
    ]
    subscription = subscriptions[0] if subscriptions else None
    if subscription:
        period = timedelta(days=RENEWAL_PERIOD_DAYS)
        end = subscription.next_billing_at
        # PAST_DUE leaves next_billing_at in the past until a retry succeeds;
        # keep metering in consecutive periods rather than an expired one.
        while now >= end:
            end += period
        # The mirror image: a `now` before the current window (the stats
        # "previous period" view passes now=start-ε) rolls back instead of
        # returning the current window again.
        while now < end - period:
            end -= period
        return end - period, end

    local = now + EAT_OFFSET
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=32)).replace(day=1)
    return start_local - EAT_OFFSET, end_local - EAT_OFFSET


def count_events(db: Session, merchant_id, kind: AmaraEventKind, start: datetime, end: datetime) -> int:
    return (
        db.query(AmaraEvent)
        .filter(
            AmaraEvent.merchant_id == merchant_id,
            AmaraEvent.kind == kind,
            AmaraEvent.created_at >= start,
            AmaraEvent.created_at < end,
        )
        .count()
    )


def effective_reply_limit(db: Session, merchant: User) -> Optional[int]:
    """This merchant's meter: their plan's limit, or the global default
    while no plan SKUs exist yet. None means unlimited (seller-exempt,
    or a plan with no limit set)."""
    from app.services.subscription_access import get_platform_plans, plan_reply_limit

    if not get_platform_plans(db):
        return settings.PLAN_REPLY_LIMIT
    return plan_reply_limit(db, merchant)


def _event_fired_in_period(db: Session, merchant_id, kind: AmaraEventKind, detail: Optional[str], start: datetime, end: datetime) -> bool:
    return (
        db.query(AmaraEvent)
        .filter(
            AmaraEvent.merchant_id == merchant_id,
            AmaraEvent.kind == kind,
            AmaraEvent.detail == detail,
            AmaraEvent.created_at >= start,
            AmaraEvent.created_at < end,
        )
        .first()
        is not None
    )


def _warn_if_threshold_crossed(db: Session, merchant_id) -> None:
    merchant = db.get(User, merchant_id)
    if not merchant:
        return
    start, end = billing_period(db, merchant)
    used = count_events(db, merchant_id, AmaraEventKind.REPLY, start, end)
    limit = effective_reply_limit(db, merchant)
    if not limit:
        return

    for percent in WARNING_THRESHOLDS_PERCENT:
        if used * 100 < percent * limit:
            continue
        if _event_fired_in_period(db, merchant_id, AmaraEventKind.USAGE_WARNING, str(percent), start, end):
            continue
        # shortcut: two replies landing at the same instant can both pass the
        # check above and send the alert twice; harmless (a duplicate alert),
        # upgrade to a unique constraint if it ever matters.
        record_event(db, merchant_id, AmaraEventKind.USAGE_WARNING, detail=str(percent))

        from app.tasks.usage_alerts import send_usage_warning
        try:
            send_usage_warning.apply_async(args=[str(merchant_id), percent])
        except Exception as e:
            logger.error(f"Failed to dispatch usage warning for merchant {merchant_id}: {e}")


def check_reply_quota(db: Session, merchant: User) -> Optional[str]:
    """
    None if Amara may reply to this merchant's customer; otherwise the
    holding message to send instead of generating a reply (#90). The
    first refusal each period is recorded (LIMIT_BLOCKED, for audit and
    the dashboard) and makes sure the merchant's 100% usage warning went
    out — a limit lowered mid-period can strand usage above the cap with
    no crossing ever firing to warn them.
    """
    try:
        limit = effective_reply_limit(db, merchant)
        if not limit:
            return None
        start, end = billing_period(db, merchant)
        used = count_events(db, merchant.id, AmaraEventKind.REPLY, start, end)
        if used < limit:
            return None
        if not _event_fired_in_period(db, merchant.id, AmaraEventKind.USAGE_WARNING, "100", start, end):
            record_event(db, merchant.id, AmaraEventKind.USAGE_WARNING, detail="100")
            from app.tasks.usage_alerts import send_usage_warning
            try:
                send_usage_warning.apply_async(args=[str(merchant.id), 100])
            except Exception as e:
                logger.error(f"Failed to dispatch usage warning for merchant {merchant.id}: {e}")
        if not _event_fired_in_period(db, merchant.id, AmaraEventKind.LIMIT_BLOCKED, None, start, end):
            record_event(db, merchant.id, AmaraEventKind.LIMIT_BLOCKED)
        return QUOTA_EXHAUSTED_HOLDING_MESSAGE
    except Exception:
        # The ledger must never silence a merchant: any failure here fails
        # open (reply as normal), logged loudly since it may be over-quota.
        db.rollback()
        logger.exception(f"Reply quota check failed for merchant {merchant.id}")
        return None
