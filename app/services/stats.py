from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    InstagramAccount,
    InstagramDirectMessage,
    Message,
    Order,
    OrderStatus,
    User,
    WhatsAppAccount,
)
from app.schemas.stats import MerchantStats, ProductStats, ReplyUsage
from app.services import usage

TOP_PRODUCTS = 5


def merchant_stats(db: Session, merchant: User, period: Literal["current", "previous"] = "current") -> MerchantStats:
    start, end = usage.billing_period(db, merchant)
    if period == "previous":
        start, end = usage.billing_period(db, merchant, now=start - timedelta(microseconds=1))

    replies_used = usage.count_events(db, merchant.id, AmaraEventKind.REPLY, start, end)
    # The dashboard must show the meter enforcement actually uses (#90) —
    # the plan's limit, not the global default. Unlimited (seller-exempt,
    # no limit set) falls back to the global for display, same as
    # app/tasks/usage_alerts.py.
    limit = usage.effective_reply_limit(db, merchant) or settings.PLAN_REPLY_LIMIT

    orders = (
        db.query(Order)
        .outerjoin(WhatsAppAccount, Order.account_id == WhatsAppAccount.id)
        .outerjoin(InstagramAccount, Order.instagram_account_id == InstagramAccount.id)
        .filter(
            or_(WhatsAppAccount.user_id == merchant.id, InstagramAccount.user_id == merchant.id),
            Order.created_at >= start,
            Order.created_at < end,
        )
        .all()
    )
    orders_paid = sum(1 for o in orders if o.status == OrderStatus.PAID)
    buyers = _buyer_count(db, merchant, start, end)

    tzs_collected = (
        db.query(func.coalesce(func.sum(AmaraEvent.amount), 0.0))
        .filter(
            AmaraEvent.merchant_id == merchant.id,
            AmaraEvent.kind == AmaraEventKind.PAYMENT,
            AmaraEvent.created_at >= start,
            AmaraEvent.created_at < end,
        )
        .scalar()
    )

    return MerchantStats(
        period_start=start,
        period_end=end,
        replies=ReplyUsage(
            used=replies_used, limit=limit, percent_used=round(replies_used * 100 / limit, 1) if limit else 0.0
        ),
        buyers=buyers,
        handoffs=usage.count_events(db, merchant.id, AmaraEventKind.HANDOFF, start, end),
        orders_created=len(orders),
        orders_paid=orders_paid,
        tzs_collected=float(tzs_collected),
        conversion_rate=round(orders_paid / buyers, 3) if buyers else None,
        top_products=_top_products(db, merchant, orders, start, end),
    )


def _buyer_count(db: Session, merchant: User, start: datetime, end: datetime) -> int:
    """Distinct WhatsApp numbers plus distinct Instagram DM customers who messaged in the period."""
    own_phones = {normalize_phone(p) for p in (merchant.username, merchant.phone_number) if p}
    whatsapp_phones = {
        normalize_phone(phone)
        for (phone,) in db.query(Message.from_phone)
        .join(WhatsAppAccount, Message.account_id == WhatsAppAccount.id)
        .filter(
            WhatsAppAccount.user_id == merchant.id,
            Message.direction == "inbound",
            Message.timestamp >= start,
            Message.timestamp < end,
        )
        .distinct()
        if phone
    }
    instagram_customers = (
        db.query(func.count(func.distinct(InstagramDirectMessage.customer_id)))
        .join(InstagramAccount, InstagramDirectMessage.account_id == InstagramAccount.id)
        .filter(
            InstagramAccount.user_id == merchant.id,
            InstagramDirectMessage.direction == "inbound",
            InstagramDirectMessage.created_at >= start,
            InstagramDirectMessage.created_at < end,
        )
        .scalar()
    )
    # The merchant texting their own number is a command, not a buyer.
    return len(whatsapp_phones - own_phones) + (instagram_customers or 0)


def _top_products(db: Session, merchant: User, orders: list, start: datetime, end: datetime) -> list:
    quotes = Counter(
        name
        for (name,) in db.query(AmaraEvent.product_name).filter(
            AmaraEvent.merchant_id == merchant.id,
            AmaraEvent.kind == AmaraEventKind.QUOTE,
            AmaraEvent.created_at >= start,
            AmaraEvent.created_at < end,
        )
        if name
    )
    order_counts = Counter(o.product.name for o in orders if o.product)
    paid_counts = Counter(o.product.name for o in orders if o.product and o.status == OrderStatus.PAID)

    names = set(quotes) | set(order_counts)
    ranked = sorted(names, key=lambda n: (paid_counts[n], order_counts[n], quotes[n]), reverse=True)
    return [
        ProductStats(product=n, quotes=quotes[n], orders=order_counts[n], paid_orders=paid_counts[n])
        for n in ranked[:TOP_PRODUCTS]
    ]
