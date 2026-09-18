import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import Customer, Order, OrderStatus, PaymentType, Product, ProductSubscription, User
from app.services.subscription_access import get_platform_subscription_product

logger = logging.getLogger(__name__)

# Mirrors LLMService._close_deal's reservation window (app/services/llm.py)
# — kept in sync deliberately rather than imported, since this flow is
# otherwise fully independent of LLMService.
RESERVATION_WINDOW = timedelta(hours=48)


class NoPlatformProductConfiguredError(Exception):
    """PLATFORM_SUBSCRIPTION_PRODUCT_SLUG is unset or points at a missing Product."""


class PhoneNumberRequiredError(Exception):
    """No phone number available (not on file, and none supplied for this payment)."""


class PhoneNumberInUseError(Exception):
    """The supplied phone number is already on a different User's account."""


def get_my_subscription(db: Session, user: User) -> Optional[ProductSubscription]:
    from app.services.subscription_access import find_platform_subscription

    return find_platform_subscription(db, user)


async def pay_now(db: Session, user: User, phone_number: Optional[str]) -> dict:
    """
    Dashboard-native "Pay Now" for Taskflow's own subscription — no AI
    negotiation needed since the price is fixed (product.max_price ==
    min_price by convention for a subscription product; this charges
    max_price regardless). Always creates a fresh Order/ProductSubscription
    pair, the same flow whether this is a first-ever payment, catching up a
    lapsed (PAST_DUE) one, or resubscribing after CANCELLED — no special-
    casing, per product decision on #5. Activation happens the normal way,
    via the Tunzaa webhook's existing order-<id> COMPLETED handling
    (app/api/v1/tunzaa_webhook.py's _activate_subscription_if_linked).
    """
    from app.services.tunzaa import tunzaa_service

    product = get_platform_subscription_product(db)
    if not product:
        raise NoPlatformProductConfiguredError()

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

    price = product.max_price
    if product.quantity > 0:
        product.quantity -= 1
    order = Order(
        customer_id=customer.id,
        product_id=product.id,
        quantity=1,
        agreed_price=price,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
        reserved_until=datetime.utcnow() + RESERVATION_WINDOW,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    db.add(
        ProductSubscription(
            order_id=order.id, customer_id=customer.id, product_id=product.id, monthly_amount=price,
        )
    )
    db.commit()

    try:
        resp = await tunzaa_service.initiate_payment(
            phone=normalized_phone, amount=price, reference=f"order-{order.id}"
        )
    except Exception as e:
        logger.error(f"Pay Now dispatch failed for order {order.id}: {e}")
        _release_failed_order(db, order, product)
        raise

    transaction_id = resp.get("transactionID")
    if not transaction_id:
        logger.error(f"Pay Now dispatch returned no transaction id for order {order.id}: {resp}")
        _release_failed_order(db, order, product)
        raise RuntimeError("Tunzaa did not return a transaction id")

    order.tunzaa_transaction_id = transaction_id
    db.commit()

    return {
        "order_id": order.id,
        "message": f"A payment request for TZS {price:,.0f} has been sent to {normalized_phone} — "
        "approve it on your phone to activate your subscription.",
    }


def _release_failed_order(db: Session, order: Order, product: Product) -> None:
    product.quantity += order.quantity or 1
    order.status = OrderStatus.CANCELLED
    db.commit()
