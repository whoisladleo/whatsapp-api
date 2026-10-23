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
    from app.services.subscription_access import find_platform_subscription

    now = now or datetime.utcnow()
    subscription = find_platform_subscription(db, merchant)
    if (
        subscription
        and subscription.next_billing_at
        and subscription.status in (ProductSubscriptionStatus.ACTIVE, ProductSubscriptionStatus.PAST_DUE)
    ):
        period = timedelta(days=RENEWAL_PERIOD_DAYS)
        end = subscription.next_billing_at
        # PAST_DUE leaves next_billing_at in the past until a retry succeeds;
        # keep metering in consecutive periods rather than an expired one.
        while now >= end:
            end += period
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


def _warn_if_threshold_crossed(db: Session, merchant_id) -> None:
    merchant = db.get(User, merchant_id)
    if not merchant:
        return
    start, end = billing_period(db, merchant)
    used = count_events(db, merchant_id, AmaraEventKind.REPLY, start, end)
    limit = settings.PLAN_REPLY_LIMIT

    for percent in WARNING_THRESHOLDS_PERCENT:
        if used * 100 < percent * limit:
            continue
        already_warned = (
            db.query(AmaraEvent)
            .filter(
                AmaraEvent.merchant_id == merchant_id,
                AmaraEvent.kind == AmaraEventKind.USAGE_WARNING,
                AmaraEvent.detail == str(percent),
                AmaraEvent.created_at >= start,
                AmaraEvent.created_at < end,
            )
            .first()
        )
        if already_warned:
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
