import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import (
    Customer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    User,
)
from app.services import product_subscription

logger = logging.getLogger(__name__)

# Mirrors LLMService._close_deal's reservation window (app/services/llm.py)
# — kept in sync deliberately rather than imported, since this flow is
# otherwise fully independent of LLMService.
RESERVATION_WINDOW = timedelta(hours=48)


class NoPlatformProductConfiguredError(Exception):
    """No Product is flagged as a platform plan SKU."""


class UnknownPlanError(Exception):
    """The requested product_id isn't a platform plan SKU."""


class PlanChoiceRequiredError(Exception):
    """Several plans exist and the request didn't pick one."""


class PhoneNumberRequiredError(Exception):
    """No phone number available (not on file, and none supplied for this payment)."""


class PhoneNumberInUseError(Exception):
    """The supplied phone number is already on a different User's account."""


class AlreadySubscribedError(Exception):
    """An ACTIVE subscription already exists for this customer+product (#115)."""


def get_my_subscription(db: Session, user: User) -> Optional[ProductSubscription]:
    from app.services.subscription_access import find_platform_subscription

    return find_platform_subscription(db, user)


async def pay_now(
    db: Session, user: User, phone_number: Optional[str], product_id=None
) -> dict:
    """
    Dashboard-native "Pay Now" for Taskflow's own subscription — no AI
    negotiation needed since the price is fixed (product.max_price ==
    min_price by convention for a subscription product; this charges
    max_price regardless). Same flow whether this is a first-ever payment,
    catching up a lapsed (PAST_DUE) one, or resubscribing after CANCELLED
    — no special-casing, per product decision on #5 — except that a
    repeat click while the previous attempt is still unpaid reuses that
    pending Order instead of minting a second one (#110). Activation
    happens the normal way, via the Tunzaa webhook's existing order-<id>
    COMPLETED handling (app/api/v1/tunzaa_webhook.py's
    _activate_subscription_if_linked).
    """
    from app.services.subscription_access import get_platform_plans
    from app.services.tunzaa import tunzaa_service

    plans = get_platform_plans(db)
    if not plans:
        raise NoPlatformProductConfiguredError()
    if product_id is not None:
        product = next((p for p in plans if str(p.id) == str(product_id)), None)
        if not product:
            raise UnknownPlanError()
    elif len(plans) > 1:
        raise PlanChoiceRequiredError()
    else:
        product = plans[0]

    phone = phone_number or user.phone_number or user.username
    if not phone:
        raise PhoneNumberRequiredError()
    normalized_phone = normalize_phone(phone)

    if phone_number:
        # Entered inline for this payment — persist it so future matching
        # (renewals, access gating) keeps resolving back to this User.
        existing = db.query(User).filter(User.phone_number == normalized_phone).first()
        if existing and existing.id != user.id:
            raise PhoneNumberInUseError()
        user.phone_number = normalized_phone
        db.commit()

    customer = db.query(Customer).filter(Customer.phone_number == normalized_phone).first()
    if not customer:
        customer = Customer(phone_number=normalized_phone)
        db.add(customer)
        db.commit()
        db.refresh(customer)

    # Paying for a plan that's already ACTIVE is always a duplicate charge
    # (#115) — the dashboard hides the button, but the API must refuse too.
    # Deliberately ACTIVE-only: PAST_DUE is a legitimate catch-up,
    # CANCELLED/PAYMENT_FAILED a legitimate resubscribe, and a
    # PENDING_PAYMENT row with no live order means a dead prompt worth
    # retrying (the reuse query above handles the live one).
    active = (
        db.query(ProductSubscription)
        .filter(
            ProductSubscription.customer_id == customer.id,
            ProductSubscription.product_id == product.id,
            ProductSubscription.status == ProductSubscriptionStatus.ACTIVE,
        )
        .first()
    )
    if active:
        logger.warning(
            "Pay Now refused: subscription %s already ACTIVE for customer %s / product %s.",
            active.id,
            customer.id,
            product.id,
        )
        raise AlreadySubscribedError()

    price = product.max_price
    # Idempotency (#110): a repeat click while the first prompt is still
    # unpaid must not mint a second Order — two orders means two real
    # charges, and only one subscription row ever resolves, so the second
    # payment would be silently absorbed. Reuse the still-pending FULL
    # order; a fresh Tunzaa prompt goes out against it below. Installment
    # orders are never reused (a Tunzaa plan's legs would keep billing
    # against a full-pay prompt), nor multi-unit ones (their agreed price
    # is a negotiated total, not this unit price).
    order = _find_reusable_order(db, customer, product)
    stock_taken = False
    fresh_order = False
    if order is None:
        # Subscriptions have no units — never claim stock for them, even
        # when quantity drifted above zero (pre-#118 phantoms or a manual
        # merchant edit). stock_taken stays False, so the release path
        # below restores nothing either (#119).
        if not product.is_subscription:
            # Atomic conditional claim, like _close_deal's (P12): a
            # check-then-decrement here let two customers racing the last
            # unit both succeed (quantity went to -1). Called via the
            # facade so tests patching app.services.llm.* keep landing.
            from app.services import llm as llm_facade
            stock_taken = llm_facade._try_claim_stock(db, product)
        order = Order(
            customer_id=customer.id,
            product_id=product.id,
            buyer_user_id=user.id,
            quantity=1,
            agreed_price=price,
            payment_type=PaymentType.FULL,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + RESERVATION_WINDOW,
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            # Lost a double-click race — the other click's PENDING order
            # committed first and the partial unique index rejected ours
            # (#114). Unlike the old inline decrement, the atomic claim
            # above committed separately, so the rollback does NOT undo
            # it — give the unit back explicitly, like _race_loser_note,
            # or every lost race leaks one unit.
            db.rollback()
            if stock_taken:
                product.quantity = (product.quantity or 0) + 1
                db.commit()
            logger.warning(
                "Pay Now lost a double-click race for customer %s / product %s; reusing the winner.",
                customer.id,
                product.id,
            )
            stock_taken = False
            order = _find_reusable_order(db, customer, product)
            if order is None:
                raise
        else:
            fresh_order = True
    if fresh_order:
        db.refresh(order)

        # The decrement above may have taken the last unit.
        from app.tasks.catalog_sync import dispatch_catalog_sync
        dispatch_catalog_sync(product.id, "upsert")
    else:
        order.agreed_price = price
        order.reserved_until = datetime.utcnow() + RESERVATION_WINDOW
        db.commit()
        db.refresh(order)

    # Reuses an existing unpaid attempt for this customer+product rather
    # than inserting a new row every time Pay Now is clicked — see
    # product_subscription.get_or_create_pending_subscription.
    product_subscription.get_or_create_pending_subscription(db, order, product, customer.id)

    # Dashboard orders never scheduled this (only chat closes did), so an
    # abandoned Pay Now prompt sat PENDING forever. On the reuse path this
    # reschedules, extending the deadline from the latest click — matching
    # the refreshed reserved_until above; the older firing no-ops on the
    # reservation check (#110).
    try:
        from app.tasks.order_reminders import AUTO_CANCEL_DELAY_SECONDS, auto_cancel_order
        auto_cancel_order.apply_async(
            args=[str(order.id)], countdown=AUTO_CANCEL_DELAY_SECONDS
        )
    except Exception as e:
        logger.error(f"Failed to schedule auto-cancel for order {order.id}: {e}")

    try:
        resp = await tunzaa_service.initiate_payment(
            phone=normalized_phone, amount=price, reference=f"order-{order.id}"
        )
    except Exception as e:
        logger.error(f"Pay Now dispatch failed for order {order.id}: {e}")
        _release_failed_order(db, order, product, stock_taken)
        raise

    transaction_id = resp.get("transactionID")
    if not transaction_id:
        logger.error(f"Pay Now dispatch returned no transaction id for order {order.id}: {resp}")
        _release_failed_order(db, order, product, stock_taken)
        raise RuntimeError("Tunzaa did not return a transaction id")

    order.tunzaa_transaction_id = transaction_id
    db.commit()

    return {
        "order_id": order.id,
        "message": f"A payment request for TZS {price:,.0f} has been sent to {normalized_phone} — "
        "approve it on your phone to activate your subscription.",
    }


# Chat confirmations for the subscription nudge ("just say the word" twin
# to the dashboard Pay Now button): accepted answers, matched
# case-insensitively after stripping.
PAY_CONFIRM_WORDS = frozenset({"yes", "ndiyo"})


def is_pay_confirmation(text: str | None) -> bool:
    """Normalized YES/NDIYO match, shared by the typed-text path and the
    nudge quick-reply tap path (tap id and title are both checked — Meta
    drifts which one carries the configured response)."""
    return (text or "").strip().lower() in PAY_CONFIRM_WORDS


async def confirm_subscription_payment(
    db: Session, user: User, sender_phone: str, message_body: str
) -> Optional[str]:
    """
    Reply-YES fast path for subscription payment. Returns the
    merchant-facing reply, or None when this message isn't a pay
    confirmation (caller falls through to normal handling).

    Same order-<id> reference and pending-order reuse as pay_now — this
    calls pay_now itself, so a YES while a prompt is still unpaid just
    re-fires against the live order instead of minting a second charge
    (#110). Throttled like a merchant resend: a YES inside
    RESEND_MIN_INTERVAL of the last dispatch gets a wait note, not a
    second prompt.
    """
    if not is_pay_confirmation(message_body):
        return None

    from app.services.subscription_access import get_platform_plans

    plans = get_platform_plans(db)
    if not plans:
        return None  # gating off — a YES means nothing here; fall through
    if len(plans) > 1:
        dashboard = settings.DASHBOARD_URL.rstrip("/")
        return (
            "You've got more than one plan to choose from — "
            f"pick yours here and tap Pay: {dashboard}/dashboard/settings"
        )
    product = plans[0]

    normalized_phone = normalize_phone(sender_phone)
    customer = db.query(Customer).filter(Customer.phone_number == normalized_phone).first()
    if customer is not None:
        live = _find_reusable_order(db, customer, product)
        if live is not None and live.tunzaa_transaction_id and live.updated_at:
            from app.services.llm import RESEND_MIN_INTERVAL

            elapsed = datetime.utcnow() - live.updated_at
            if elapsed < RESEND_MIN_INTERVAL:
                wait_mins = int((RESEND_MIN_INTERVAL - elapsed).total_seconds() // 60) + 1
                return (
                    "A payment prompt is already on its way to your phone — "
                    f"check it (or wait ~{wait_mins} min and say YES again)."
                )

    try:
        result = await pay_now(db, user, normalized_phone)
    except AlreadySubscribedError:
        return "Your subscription is already active — Amara is on."
    except (PhoneNumberRequiredError, PhoneNumberInUseError):
        dashboard = settings.DASHBOARD_URL.rstrip("/")
        return (
            "I couldn't match your number to your account — "
            f"pay from the dashboard instead: {dashboard}/dashboard/settings"
        )
    except Exception:
        logger.exception(f"Chat pay-confirm failed for user {user.id}")
        return "I couldn't reach the payment network — try YES again in a bit."

    return result["message"]


def _find_reusable_order(db: Session, customer: Customer, product: Product):
    """Latest still-pending FULL single-unit order for this customer+product, if any."""
    return (
        db.query(Order)
        .filter(
            Order.customer_id == customer.id,
            Order.product_id == product.id,
            Order.status == OrderStatus.PENDING,
            Order.payment_type == PaymentType.FULL,
            Order.tunzaa_plan_id.is_(None),
            Order.quantity == 1,
        )
        .order_by(Order.created_at.desc())
        .first()
    )


def _release_failed_order(
    db: Session, order: Order, product: Product, restore_stock: bool
) -> None:
    # Only give back stock this attempt actually took: a reused pending
    # order took none, and neither did a fresh one when the shelf was
    # already empty — restoring blindly would mint phantom units (#110).
    if restore_stock:
        product.quantity += order.quantity or 1
    order.status = OrderStatus.CANCELLED
    db.commit()
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(product.id, "upsert")
