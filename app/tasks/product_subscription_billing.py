import logging
from datetime import datetime

from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.db.session import SessionLocal
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def charge_renewal(subscription_id: str):
    """Self-rescheduling monthly billing task for a ProductSubscription —
    see app.services.product_subscription.handle_renewal_payment_result for
    what happens once the Tunzaa webhook confirms whether this charge
    succeeded (including rescheduling the *next* renewal on success)."""
    return _run_async(_do_charge_renewal(subscription_id))


async def _do_charge_renewal(subscription_id: str) -> None:
    from app.db.models import ProductSubscription, ProductSubscriptionStatus
    from app.services import product_subscription
    from app.services.tunzaa import tunzaa_service

    db = SessionLocal()
    try:
        subscription_uuid = coerce_uuid(subscription_id)
        if subscription_uuid is None:
            logger.error(f"charge_renewal: subscription {subscription_id} not found")
            return
        subscription = (
            db.query(ProductSubscription).filter(ProductSubscription.id == subscription_uuid).first()
        )
        if not subscription:
            logger.error(f"charge_renewal: subscription {subscription_id} not found")
            return
        if subscription.status not in (
            ProductSubscriptionStatus.ACTIVE,
            ProductSubscriptionStatus.PAST_DUE,
        ):
            # Cancelled or otherwise no longer billable in the meantime —
            # don't charge a subscription nobody expects to be charged.
            return

        customer = subscription.customer
        if not customer:
            logger.error(f"charge_renewal: subscription {subscription_id} has no customer")
            return

        if product_subscription._current_cycle_already_paid(subscription):
            # The cycle is paid and not due — a sibling merge pushed the
            # date forward, or this task was scheduled twice. Pushing now
            # would be a duplicate charge; re-sleep until the date instead
            # of billing. PAST_DUE rows never land here (the helper is
            # ACTIVE-only), so retries are unaffected (#152).
            delay = (subscription.next_billing_at - datetime.utcnow()).total_seconds()
            try:
                charge_renewal.apply_async(args=[str(subscription.id)], countdown=max(delay, 0))
            except Exception as sched_err:
                logger.error(
                    f"Failed to reschedule not-yet-due renewal for subscription {subscription_id}: {sched_err}"
                )
            return

        try:
            await tunzaa_service.initiate_payment(
                phone=customer.phone_number,
                amount=subscription.monthly_amount,
                reference=f"product-subscription-renewal-{subscription.id}",
            )
        except Exception as e:
            # The push never left, so no webhook will ever arrive for it —
            # logging alone ends billing silently (ACTIVE stays ACTIVE and
            # is never charged again). Flow through the FAILED branch like
            # any other failed charge: bounded retries, both sides told (#152).
            logger.error(f"Failed to push renewal charge for subscription {subscription_id}: {e}")
            await product_subscription.handle_renewal_payment_result(db, subscription, "FAILED", None)
    finally:
        db.close()


@celery_app.task
def check_past_due_grace_expired(subscription_id: str):
    """
    Scheduled once, exactly PAST_DUE_ACCESS_GRACE_DAYS out, the first time a
    subscription goes PAST_DUE (see
    product_subscription.handle_renewal_payment_result) — nothing else
    re-evaluates access at that precise moment otherwise, since renewal
    retries run on their own independent 36h cadence. A no-op if the
    subscription already recovered (ACTIVE) or was cancelled by then.
    """
    _run_async(_do_check_past_due_grace_expired(subscription_id))


async def _do_check_past_due_grace_expired(subscription_id: str) -> None:
    from app.db.models import ProductSubscription, ProductSubscriptionStatus
    from app.services import product_subscription

    db = SessionLocal()
    try:
        subscription_uuid = coerce_uuid(subscription_id)
        if subscription_uuid is None:
            logger.error(f"check_past_due_grace_expired: subscription {subscription_id} not found")
            return
        subscription = (
            db.query(ProductSubscription).filter(ProductSubscription.id == subscription_uuid).first()
        )
        if not subscription:
            logger.error(f"check_past_due_grace_expired: subscription {subscription_id} not found")
            return
        if subscription.status != ProductSubscriptionStatus.PAST_DUE:
            return

        product_subscription.sync_access_if_platform_subscription(db, subscription)
    finally:
        db.close()


# Reconcile retries when a Tunzaa plan fetch fails mid-webhook (see
# tunzaa_webhook._reconcile_installment_plan): 5 attempts, 5 minutes
# apart. The paid leg is already recorded — these only settle the
# order-level state once Tunzaa answers again.
RECONCILE_RETRY_DELAY_SECONDS = 5 * 60
RECONCILE_MAX_ATTEMPTS = 5


@celery_app.task
def retry_installment_reconcile(order_id: str, attempt: int):
    """
    Re-runs an installment-plan reconcile whose Tunzaa fetch failed during
    the webhook. Self-reschedules until RECONCILE_MAX_ATTEMPTS, then gives
    up loudly with the order still PENDING — a human-visible state, never
    a silently-paid one. No-op if the order resolved in the meantime.
    """
    return _run_async(_do_retry_installment_reconcile(order_id, attempt))


async def _do_retry_installment_reconcile(order_id: str, attempt: int) -> None:
    from app.api.v1.tunzaa_webhook import apply_installment_plan_state
    from app.db.models import Order, OrderStatus
    from app.services.tunzaa import tunzaa_service

    db = SessionLocal()
    try:
        order_uuid = coerce_uuid(order_id)
        if order_uuid is None:
            logger.error(f"retry_installment_reconcile: order {order_id} not found")
            return
        order = db.query(Order).filter(Order.id == order_uuid).first()
        if not order:
            logger.error(f"retry_installment_reconcile: order {order_id} not found")
            return
        if order.status != OrderStatus.PENDING or not order.tunzaa_plan_id:
            # Resolved (paid/failed/cancelled) or never had a plan — nothing to do.
            return
        try:
            plan_data = await tunzaa_service.get_installment_plan(
                order.tunzaa_plan_id, include_payments=True
            )
        except Exception as e:
            if attempt < RECONCILE_MAX_ATTEMPTS:
                try:
                    retry_installment_reconcile.apply_async(
                        args=[order_id, attempt + 1],
                        countdown=RECONCILE_RETRY_DELAY_SECONDS,
                    )
                except Exception as sched_err:
                    logger.error(
                        f"Failed to reschedule reconcile retry for order {order_id}: {sched_err}"
                    )
            else:
                logger.error(
                    f"retry_installment_reconcile: plan fetch failed "
                    f"{RECONCILE_MAX_ATTEMPTS}x for order {order_id} ({e}); "
                    "leaving PENDING for merchant review."
                )
                # A log line alone meant the order was later auto-cancelled
                # despite possibly being fully paid, with nobody told — the
                # give-up must page the merchant with the order and the
                # amounts on record. Best-effort: never crash the task. (P9)
                try:
                    from app.api.v1.tunzaa_webhook import notify_merchant_of_reconcile_giveup
                    await notify_merchant_of_reconcile_giveup(
                        db, order, RECONCILE_MAX_ATTEMPTS
                    )
                except Exception as alert_err:
                    logger.error(
                        f"retry_installment_reconcile: give-up alert failed "
                        f"for order {order_id}: {alert_err}"
                    )
            return
        apply_installment_plan_state(db, order, plan_data)
        db.commit()
        if order.status == OrderStatus.PAID:
            # The retry is a second path to PAID (SNPL fully paid off while
            # the webhook's plan fetch was down) — run the same activation
            # and completion alert the webhook path runs, or a fully-paid
            # plan strands in PENDING_PAYMENT with nobody told (#152).
            from app.api.v1.tunzaa_webhook import (
                _activate_subscription_if_linked,
                _notify_merchant_of_payment,
            )
            await _activate_subscription_if_linked(db, order)
            await _notify_merchant_of_payment(db, order)
    finally:
        db.close()
