import logging
import math
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.crud.order import try_release_pending_order
from app.db.models import (
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
)
from app.services.llm.constants import (
    HANDOFF_ALERT_MESSAGE_MAX_CHARS,
    NEGOTIATION_FLOOR_BUFFER,
    RESERVATION_WINDOW,
)
from app.services.llm.prompts import _ALREADY_ACTIVE_LINES, _ALREADY_SENT_LINES

logger = logging.getLogger("app.services.llm")


def _hold_expired(order: Order) -> bool:
    """
    True when a PENDING order's payment hold has lapsed: past
    reserved_until, or (when that's NULL — old rows) older than the
    reservation window. Rows with no timestamp at all fail safe toward
    "still live". Shared by the reclose guard's lazy-cancel and the
    resend path's live-order lookup (#113).
    """
    deadline = order.reserved_until
    if deadline is None and order.created_at is not None:
        deadline = order.created_at + RESERVATION_WINDOW
    if deadline is None:
        return False
    return deadline <= datetime.utcnow()


def _truncate_for_alert(text: str, max_chars: int = HANDOFF_ALERT_MESSAGE_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _negotiation_floor(product: Product) -> float:
    return min(product.max_price, math.ceil(product.min_price * NEGOTIATION_FLOOR_BUFFER))


def _is_out_of_stock(product: Product) -> bool:
    """
    Whether the catalog should show this product as OUT OF STOCK: no units
    left (quantity < 1, e.g. via /out_of_stock) or deactivated by the
    merchant (is_active False). Either way the model must not offer it —
    see the out-of-stock RULES line in each prompt. Server-side
    _close_deal/_close_instagram_deal still reject a quantity-0 close as
    the backstop (#53). Subscription products are never out of stock —
    they have no units to deplete (see #89).
    """
    if product.is_subscription:
        return product.is_active is False
    return (product.quantity or 0) < 1 or product.is_active is False


def _try_claim_stock(db: Session, product: Product) -> bool:
    """
    Atomically claims one unit of stock via a conditional UPDATE
    (SET quantity = quantity - 1 WHERE id = ... AND quantity >= 1) —
    the same claim style as claim_available_pool_number: the
    affected-row count tells us whether we won the race, correct on
    both SQLite tests and Postgres prod (#54). Returns True when this
    call took the unit; False (0 rows) means a concurrent close took
    the last unit first, and the caller must reject the deal exactly
    like the out-of-stock pre-check. Refreshes the ORM object on
    success so callers see the post-claim quantity.
    """
    claimed = (
        db.query(Product)
        .filter(Product.id == product.id, Product.quantity >= 1)
        # synchronize_session=False: the SET value is a SQL expression
        # (quantity - 1), not a literal, so there is no Python-side value
        # to copy onto in-session objects — refresh below instead.
        .update({Product.quantity: Product.quantity - 1}, synchronize_session=False)
    )
    db.commit()
    if claimed == 0:
        return False
    db.refresh(product)
    if (product.quantity or 0) < 1:
        # The claim just took the last unit, so the catalog's availability
        # flipped to out of stock — sync it. Non-zero claims change no
        # catalog field (quantity itself is never sent), so they sync nothing.
        from app.tasks.catalog_sync import dispatch_catalog_sync
        dispatch_catalog_sync(product.id, "upsert")
    return True


class DealRejected(Exception):
    """An AGREEMENT_REACHED marker failed server-side validation; no order was created."""


def _lang_text(variants: dict, language: Optional[str]) -> str:
    """
    Picks the customer's language variant of a template note; both
    stacked (Swahili first) when the language isn't known — the same
    fallback _holding_reply/_outage_reply use. Instagram never
    classifies Customer.language, so IG notes are bilingual.
    """
    if language in variants:
        return variants[language]
    return f"{variants['sw']}\n\n{variants['en']}"


def _reclose_guard_note(
    db: Session, customer_id, product: Product, channel_filter, language: Optional[str] = None
) -> Optional[str]:
    """
    Fail-closed guard against duplicate closes (#101): the model
    re-emits AGREEMENT_REACHED across turns (seen 3x in one sim
    conversation), and every emission used to mint a fresh PENDING
    order plus a fresh Tunzaa dispatch — duplicate charge prompts for
    a single deal. Returns the customer-facing note when this close
    must NOT proceed, else None. Only a LIVE pending order blocks — an
    expired hold is retired on the spot (auto-cancel's countdown firing
    doesn't survive a redeploy, so "the task cleans those up" stopped
    being something we can rely on — #113). For subscriptions, any PAID
    order also blocks: a paid-for plan is owned even before its
    subscription row reaches ACTIVE (#114).
    """
    open_order = (
        db.query(Order)
        .filter(
            Order.customer_id == customer_id,
            Order.product_id == product.id,
            channel_filter,
            Order.status == OrderStatus.PENDING,
        )
        .first()
    )
    if open_order:
        if _hold_expired(open_order):
            # The hold lapsed but auto-cancel never reaped it — countdown
            # tasks don't survive a redeploy, so expired PENDING rows
            # linger (seen live: weeks old) and would wedge every future
            # close for this customer+product behind "already sent".
            # Retire it here, exactly as the missed firing would have
            # (minus the "reservation expired" notice — this turn's own
            # close note covers the UX), then fall through and close
            # fresh. The linked PENDING_PAYMENT subscription, if any, is
            # deliberately left alone: get_or_create_pending_subscription
            # re-points it at the new order (#113).
            logger.warning(
                "Reclose guard retiring expired PENDING order %s (customer %s / product %s).",
                open_order.id,
                customer_id,
                product.id,
            )
            # Atomic release (P10): a FAILED callback, auto-cancel, or
            # dashboard cancel may have resolved the hold concurrently —
            # only the transition winner restores. Either way the hold is
            # no longer a live PENDING blocker, so fall through and close
            # fresh.
            try_release_pending_order(db, open_order, OrderStatus.CANCELLED)
            db.commit()
        else:
            logger.warning(
                "Duplicate close blocked: PENDING order %s already exists for customer %s / product %s.",
                open_order.id,
                customer_id,
                product.id,
            )
            return _lang_text(_ALREADY_SENT_LINES, language).format(
                price=f"{open_order.agreed_price:,.0f}"
            )
    if product.is_subscription:
        active = (
            db.query(ProductSubscription)
            .filter(
                ProductSubscription.customer_id == customer_id,
                ProductSubscription.product_id == product.id,
                ProductSubscription.status == ProductSubscriptionStatus.ACTIVE,
            )
            .first()
        )
        if active:
            logger.warning(
                "Duplicate close blocked: subscription %s already ACTIVE for customer %s / product %s.",
                active.id,
                customer_id,
                product.id,
            )
            return _lang_text(_ALREADY_ACTIVE_LINES, language).format(product=product.name)
        # A plan already paid for is owned, even when the subscription row
        # hasn't reached ACTIVE yet (webhook lag, or a confirmation that
        # never landed) — a re-emitted close past this point is a second
        # real charge, as the sandbox 4x-PAID pile proved (#114). Keyed
        # globally per customer+product, not per channel: a plan bought
        # over WhatsApp is the same plan over Instagram. Consumables are
        # deliberately exempt — buying the same thing twice is legitimate.
        paid = (
            db.query(Order)
            .filter(
                Order.customer_id == customer_id,
                Order.product_id == product.id,
                Order.status == OrderStatus.PAID,
            )
            .first()
        )
        if paid:
            logger.warning(
                "Duplicate close blocked: PAID order %s already exists for customer %s / product %s.",
                paid.id,
                customer_id,
                product.id,
            )
            return _lang_text(_ALREADY_ACTIVE_LINES, language).format(product=product.name)
    return None


def _race_loser_note(
    db: Session,
    customer_id,
    product: Product,
    payment_type: PaymentType,
    language: Optional[str] = None,
) -> str:
    """
    Builds the already-sent note for a close that lost the double-close
    race: the partial unique index rejected this attempt's PENDING insert
    because a concurrent close committed first (#114). The caller has
    already rolled back; this restores the stock unit the loser's claim
    took (that commit landed separately, before the insert) and reads the
    winner back channel-blind — the winning order may be a channel-less
    Pay Now row the channel-scoped guard never saw — but filtered to the
    loser's payment type, since a FULL order can legitimately sit next to
    an installment one for the same deal (#110). Raises DealRejected when
    no winner exists (no winner means no constraint violation to explain,
    so something else is wrong and a human should look).
    """
    if not product.is_subscription:
        product.quantity = (product.quantity or 0) + 1
        db.commit()
    winner = (
        db.query(Order)
        .filter(
            Order.customer_id == customer_id,
            Order.product_id == product.id,
            Order.payment_type == payment_type,
            Order.status == OrderStatus.PENDING,
        )
        .order_by(Order.created_at.desc())
        .first()
    )
    if winner is None:
        raise DealRejected(
            "Close lost the double-close race but no winning PENDING order exists "
            f"for customer {customer_id} / product {product.id}"
        )
    logger.warning(
        "Duplicate close lost race: PENDING order %s won for customer %s / product %s.",
        winner.id,
        customer_id,
        product.id,
    )
    return _lang_text(_ALREADY_SENT_LINES, language).format(
        price=f"{winner.agreed_price:,.0f}"
    )
