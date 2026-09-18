import logging

from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.db.session import SessionLocal
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def provision_after_payment(subscription_id: str):
    """Dispatched from number_billing.handle_purchase_payment_result once the
    Tunzaa payment for a number purchase is confirmed. Runs the same
    Twilio-buy -> Meta-register -> verify -> activate chain used for pool
    replenishment, then claims that number for the paying account. Can take
    up to pool_provisioning.POLL_TIMEOUT_SECONDS (~2 min) — this is exactly
    why it's a background task and not run inline from the webhook."""
    return _run_async(_do_provision_after_payment(subscription_id))


async def _do_provision_after_payment(subscription_id: str) -> None:
    from datetime import datetime, timedelta

    from app.api.v1.accounts import register_phone_number, request_display_name_change
    from app.crud.account import get_pool_two_step_pin, reassign_account_to_pool_number
    from app.db.models import NumberSubscription, NumberSubscriptionStatus
    from app.services import number_billing, pool_provisioning
    from app.services.tunzaa import tunzaa_service

    db = SessionLocal()
    try:
        subscription_uuid = coerce_uuid(subscription_id)
        if subscription_uuid is None:
            logger.error(f"provision_after_payment: subscription {subscription_id} not found")
            return
        subscription = (
            db.query(NumberSubscription).filter(NumberSubscription.id == subscription_uuid).first()
        )
        if not subscription:
            logger.error(f"provision_after_payment: subscription {subscription_id} not found")
            return
        account = subscription.account

        try:
            await pool_provisioning.provision_pool_number(db, verified_name=account.name)
            account = reassign_account_to_pool_number(db, account)
            await request_display_name_change(account.phone_number_id, account.access_token, account.name)
            pin = get_pool_two_step_pin(db, account.id)
            if pin:
                await register_phone_number(account.phone_number_id, account.access_token, pin)
        except Exception as e:
            logger.error(f"Number purchase provisioning failed for subscription {subscription_id}: {e}")
            subscription.status = NumberSubscriptionStatus.PROVISIONING_FAILED
            db.commit()

            if subscription.tunzaa_transaction_id:
                try:
                    await tunzaa_service.refund_payment(
                        subscription.tunzaa_transaction_id,
                        subscription.monthly_amount,
                        reason="Number provisioning failed",
                    )
                    refund_note = "You've been refunded."
                except Exception as refund_err:
                    logger.error(f"Refund failed for subscription {subscription_id}: {refund_err}")
                    refund_note = "Please contact support for a refund."
            else:
                refund_note = "Please contact support for a refund."

            await number_billing.notify_merchant(
                db,
                account,
                f"We couldn't complete your WhatsApp number purchase. {refund_note}",
            )
            return

        subscription.status = NumberSubscriptionStatus.ACTIVE
        subscription.next_billing_at = datetime.utcnow() + timedelta(
            days=number_billing.RENEWAL_PERIOD_DAYS
        )
        db.commit()

        try:
            charge_renewal.apply_async(
                args=[str(subscription.id)],
                countdown=number_billing.RENEWAL_PERIOD_DAYS * 86400,
            )
        except Exception as e:
            logger.error(f"Failed to schedule renewal for subscription {subscription_id}: {e}")

        await number_billing.notify_merchant(
            db,
            account,
            f"🎉 Your new WhatsApp number is ready. You'll be billed "
            f"{int(subscription.monthly_amount):,} TZS/month starting today.",
        )
    finally:
        db.close()


@celery_app.task
def charge_renewal(subscription_id: str):
    """Self-rescheduling monthly billing task — see
    number_billing.handle_renewal_payment_result for what happens once the
    Tunzaa webhook confirms whether this charge succeeded."""
    return _run_async(_do_charge_renewal(subscription_id))


async def _do_charge_renewal(subscription_id: str) -> None:
    from app.db.models import NumberSubscription, NumberSubscriptionStatus
    from app.services.tunzaa import tunzaa_service

    db = SessionLocal()
    try:
        subscription_uuid = coerce_uuid(subscription_id)
        if subscription_uuid is None:
            logger.error(f"charge_renewal: subscription {subscription_id} not found")
            return
        subscription = (
            db.query(NumberSubscription).filter(NumberSubscription.id == subscription_uuid).first()
        )
        if not subscription:
            logger.error(f"charge_renewal: subscription {subscription_id} not found")
            return
        if subscription.status not in (
            NumberSubscriptionStatus.ACTIVE,
            NumberSubscriptionStatus.PAST_DUE,
        ):
            # Deactivated or otherwise no longer billable in the meantime —
            # don't charge a subscription nobody expects to be charged.
            return

        account = subscription.account
        try:
            await tunzaa_service.initiate_payment(
                phone=account.owner.username,
                amount=subscription.monthly_amount,
                reference=f"number-renewal-{subscription.id}",
            )
        except Exception as e:
            logger.error(f"Failed to push renewal charge for subscription {subscription_id}: {e}")
    finally:
        db.close()
