import logging

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def charge_renewal(subscription_id: int):
    """Self-rescheduling monthly billing task for a ProductSubscription —
    see app.services.product_subscription.handle_renewal_payment_result for
    what happens once the Tunzaa webhook confirms whether this charge
    succeeded (including rescheduling the *next* renewal on success)."""
    return _run_async(_do_charge_renewal(subscription_id))


async def _do_charge_renewal(subscription_id: int) -> None:
    from app.db.models import ProductSubscription, ProductSubscriptionStatus
    from app.services.tunzaa import tunzaa_service

    db = SessionLocal()
    try:
        subscription = (
            db.query(ProductSubscription).filter(ProductSubscription.id == subscription_id).first()
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

        try:
            await tunzaa_service.initiate_payment(
                phone=customer.phone_number,
                amount=subscription.monthly_amount,
                reference=f"product-subscription-renewal-{subscription.id}",
            )
        except Exception as e:
            logger.error(f"Failed to push renewal charge for subscription {subscription_id}: {e}")
    finally:
        db.close()
