import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.provider import WhatsAppProvider
from app.crud import message as crud_message
from app.db.models import NumberSubscription, NumberSubscriptionStatus, WhatsAppAccount

logger = logging.getLogger(__name__)

MONTHLY_AMOUNT_TZS = 10000
RENEWAL_PERIOD_DAYS = 30
RENEWAL_RETRY_DELAY_SECONDS = 24 * 60 * 60


async def start_number_purchase(db: Session, account: WhatsAppAccount) -> NumberSubscription:
    """
    Creates the billing record and pushes the first (10,000 TZS) Tunzaa
    payment request. The actual Twilio/Meta purchase only starts once that
    payment is confirmed via the Tunzaa webhook (handle_purchase_payment_result)
    — see app/tasks/number_billing.py for the rest of the chain.
    """
    from app.services.tunzaa import tunzaa_service

    subscription = NumberSubscription(
        account_id=account.id,
        status=NumberSubscriptionStatus.PENDING_PAYMENT,
        monthly_amount=MONTHLY_AMOUNT_TZS,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    await tunzaa_service.initiate_payment(
        phone=account.owner.username,
        amount=MONTHLY_AMOUNT_TZS,
        reference=f"number-purchase-{subscription.id}",
    )
    return subscription


async def notify_merchant(db: Session, account: WhatsAppAccount, body: str) -> None:
    """Sends a WhatsApp message to the merchant's own number using whatever
    credentials the account currently has (its old number, mid-purchase —
    reassignment only happens once the new number is confirmed working)."""
    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(account.owner.username, body)
    except Exception as e:
        logger.error(f"Failed to notify merchant for account {account.id}: {e}")
        resp = None

    crud_message.create_message(
        db,
        {
            "to_phone": account.owner.username,
            "type": "text",
            "body": body,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )


async def handle_purchase_payment_result(
    db: Session, subscription: NumberSubscription, status_value: str, transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "number-purchase-<id>" reference."""
    if transaction_id:
        subscription.tunzaa_transaction_id = transaction_id

    if status_value == "COMPLETED":
        subscription.status = NumberSubscriptionStatus.PROVISIONING
        db.commit()

        from app.tasks.number_billing import provision_after_payment

        try:
            provision_after_payment.apply_async(args=[subscription.id])
        except Exception as e:
            logger.error(f"Failed to schedule provision_after_payment for subscription {subscription.id}: {e}")
    elif status_value in ("FAILED", "CANCELLED"):
        subscription.status = NumberSubscriptionStatus.PAYMENT_FAILED
        db.commit()
        await notify_merchant(
            db,
            subscription.account,
            "Your WhatsApp number purchase payment wasn't completed, so no number was purchased. "
            "You can try again anytime from Settings.",
        )
    else:
        db.commit()


async def handle_renewal_payment_result(
    db: Session, subscription: NumberSubscription, status_value: str, transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "number-renewal-<id>" reference."""
    from datetime import datetime, timedelta

    from app.tasks.number_billing import charge_renewal

    account = subscription.account
    if transaction_id:
        subscription.tunzaa_transaction_id = transaction_id

    if status_value == "COMPLETED":
        subscription.status = NumberSubscriptionStatus.ACTIVE
        subscription.retry_count = 0
        subscription.next_billing_at = datetime.utcnow() + timedelta(days=RENEWAL_PERIOD_DAYS)
        db.commit()
        try:
            charge_renewal.apply_async(
                args=[subscription.id], countdown=RENEWAL_PERIOD_DAYS * 86400
            )
        except Exception as e:
            logger.error(f"Failed to schedule next renewal for subscription {subscription.id}: {e}")
    elif status_value in ("FAILED", "CANCELLED"):
        if subscription.retry_count < 1:
            subscription.status = NumberSubscriptionStatus.PAST_DUE
            subscription.retry_count += 1
            db.commit()
            try:
                charge_renewal.apply_async(
                    args=[subscription.id], countdown=RENEWAL_RETRY_DELAY_SECONDS
                )
            except Exception as e:
                logger.error(f"Failed to schedule renewal retry for subscription {subscription.id}: {e}")
            await notify_merchant(
                db,
                account,
                f"Your WhatsApp number renewal payment ({int(subscription.monthly_amount):,} TZS) "
                "failed. We'll retry in 24 hours — please make sure you can approve the mobile "
                "money prompt.",
            )
        else:
            subscription.status = NumberSubscriptionStatus.DEACTIVATED
            account.ai_enabled = False
            db.commit()
            await notify_merchant(
                db,
                account,
                "Your WhatsApp number subscription has been deactivated after repeated failed "
                "payments. Amara will stop responding to customers until you renew — visit the "
                "dashboard to reactivate.",
            )
    else:
        db.commit()
