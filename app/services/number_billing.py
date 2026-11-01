import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.provider import WhatsAppProvider
from app.crud import message as crud_message
from app.db.models import (
    AmaraEventKind,
    NumberSubscription,
    NumberSubscriptionStatus,
    WhatsAppAccount,
)
from app.services import usage

logger = logging.getLogger(__name__)

MONTHLY_AMOUNT_TZS = 10000
RENEWAL_PERIOD_DAYS = 30
RENEWAL_RETRY_DELAY_SECONDS = 24 * 60 * 60
# A renewal callback is only treated as a stale duplicate while the paid
# cycle is still more than this far from due — the 1h absorbs
# scheduler/clock skew so a genuine next-cycle signal (which always arrives
# at/after next_billing_at) is never mistaken for one.
RENEWAL_DUE_GRACE_SECONDS = 3600


async def start_number_purchase(db: Session, account: WhatsAppAccount) -> NumberSubscription:
    """
    Creates (or, retrying after PAYMENT_FAILED/PROVISIONING_FAILED/
    DEACTIVATED, reuses) the billing record and pushes the first (10,000
    TZS) Tunzaa payment request. account_id is unique on NumberSubscription
    — a retry has to reset the existing row rather than insert a second
    one, or it'd hit a DB IntegrityError. The endpoint above already blocks
    calling this at all while a subscription is PENDING_PAYMENT/
    PROVISIONING/ACTIVE/PAST_DUE, so reaching here with an existing row
    always means a legitimate retry from a terminal failure state.

    The actual Twilio/Meta purchase only starts once payment is confirmed
    via the Tunzaa webhook (handle_purchase_payment_result) — see
    app/tasks/number_billing.py for the rest of the chain.
    """
    from app.services.tunzaa import tunzaa_service

    subscription = (
        db.query(NumberSubscription).filter(NumberSubscription.account_id == account.id).first()
    )
    if subscription:
        subscription.status = NumberSubscriptionStatus.PENDING_PAYMENT
        subscription.retry_count = 0
        subscription.monthly_amount = MONTHLY_AMOUNT_TZS
    else:
        subscription = NumberSubscription(
            account_id=account.id,
            status=NumberSubscriptionStatus.PENDING_PAYMENT,
            monthly_amount=MONTHLY_AMOUNT_TZS,
        )
        db.add(subscription)
    db.commit()
    db.refresh(subscription)

    try:
        await tunzaa_service.initiate_payment(
            phone=account.owner.username,
            amount=MONTHLY_AMOUNT_TZS,
            reference=f"number-purchase-{subscription.id}",
        )
    except Exception:
        # Left in PENDING_PAYMENT otherwise — a blocking status the
        # purchase-number endpoint 409s on, so a push that never even went
        # out would permanently lock the merchant out of retrying at all.
        subscription.status = NumberSubscriptionStatus.PAYMENT_FAILED
        db.commit()
        raise

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


def _record_duplicate_purchase_payment(db: Session, subscription: NumberSubscription) -> None:
    """TZS collected from a duplicate purchase payment, for GET /stats (#41)."""
    account = subscription.account
    owner = account.owner if account else None
    usage.record_event(
        db,
        owner.id if owner else None,
        AmaraEventKind.PAYMENT,
        channel="whatsapp",
        account_id=account.id if account else None,
        amount=subscription.monthly_amount,
        detail="duplicate number purchase payment",
    )


async def handle_purchase_payment_result(
    db: Session, subscription: NumberSubscription, status_value: str, transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "number-purchase-<id>" reference."""
    already_resolved = subscription.status in (
        NumberSubscriptionStatus.PROVISIONING,
        NumberSubscriptionStatus.ACTIVE,
    )

    if status_value == "COMPLETED":
        if already_resolved:
            # A second COMPLETED with a new transaction_id is a genuine
            # double payment, not a retry (retries share the id and are
            # caught by the webhook's same-txn idempotency guard). The cash
            # is real, so record it and flag the row refund-owed — but never
            # re-provision: a second provision_after_payment would buy a
            # second number and schedule duplicate renewals. The first txn id
            # is deliberately preserved.
            subscription.refund_status = "MANUAL"
            db.commit()
            _record_duplicate_purchase_payment(db, subscription)
            await notify_merchant(
                db,
                subscription.account,
                "We received a second payment for your WhatsApp number purchase — "
                "your number is already being set up, so nothing further is needed. "
                "Please contact support for a refund of the duplicate charge.",
            )
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
        subscription.status = NumberSubscriptionStatus.PROVISIONING
        db.commit()

        from app.tasks.number_billing import provision_after_payment

        try:
            provision_after_payment.apply_async(args=[str(subscription.id)])
        except Exception as e:
            logger.error(f"Failed to schedule provision_after_payment for subscription {subscription.id}: {e}")
    elif status_value in ("FAILED", "CANCELLED"):
        if already_resolved:
            # A FAILED for an already-resolved purchase (e.g. an abandoned
            # duplicate attempt after the real one paid) must never regress
            # it — or send the false "no number was purchased" message that
            # would invite a repay double charge. Mirrors the Order branch's
            # already-resolved guard in app/api/v1/tunzaa_webhook.py.
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
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


def _current_cycle_already_paid(subscription: NumberSubscription) -> bool:
    """True when the current cycle was already paid for and isn't due yet —
    ACTIVE with retry_count reset and next_billing_at still in the future.
    A renewal callback arriving in that state is a stale duplicate for the
    paid cycle, not a genuine signal about an outstanding charge. Anything
    else — PAST_DUE, a missing date, or a due/overdue date — must be
    processed normally."""
    from datetime import datetime, timedelta

    if subscription.status != NumberSubscriptionStatus.ACTIVE:
        return False
    if subscription.retry_count != 0:
        return False
    if not subscription.next_billing_at:
        return False
    return subscription.next_billing_at > datetime.utcnow() + timedelta(
        seconds=RENEWAL_DUE_GRACE_SECONDS
    )


async def handle_renewal_payment_result(
    db: Session, subscription: NumberSubscription, status_value: str, transaction_id: Optional[str]
) -> None:
    """Called from the Tunzaa webhook for a "number-renewal-<id>" reference."""
    from datetime import datetime, timedelta

    from app.tasks.number_billing import charge_renewal

    account = subscription.account

    if status_value == "COMPLETED":
        if _current_cycle_already_paid(subscription):
            # P4: duplicate COMPLETED for the already-paid cycle — the
            # renewal task is already outstanding, so don't move
            # next_billing_at or schedule a second one (two tasks would each
            # push next cycle). Whether Tunzaa dedupes same-reference pushes
            # is unverified, so single-task scheduling is enforced here
            # rather than assumed there.
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
        subscription.status = NumberSubscriptionStatus.ACTIVE
        subscription.retry_count = 0
        subscription.next_billing_at = datetime.utcnow() + timedelta(days=RENEWAL_PERIOD_DAYS)
        db.commit()
        try:
            charge_renewal.apply_async(
                args=[str(subscription.id)], countdown=RENEWAL_PERIOD_DAYS * 86400
            )
        except Exception as e:
            logger.error(f"Failed to schedule next renewal for subscription {subscription.id}: {e}")
    elif status_value in ("FAILED", "CANCELLED"):
        if _current_cycle_already_paid(subscription):
            # P3: stale FAILED for the already-paid cycle — must not regress
            # to PAST_DUE or schedule a retry (that double-charges at 24h +
            # 30d). Genuine next-cycle failures arrive once next_billing_at
            # is due, so they never hit this path.
            return
        if transaction_id:
            subscription.tunzaa_transaction_id = transaction_id
        if subscription.retry_count < 1:
            subscription.status = NumberSubscriptionStatus.PAST_DUE
            subscription.retry_count += 1
            db.commit()
            try:
                charge_renewal.apply_async(
                    args=[str(subscription.id)], countdown=RENEWAL_RETRY_DELAY_SECONDS
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
        # Unknown/missing status: must NOT record the txn id — the
        # webhook's same-txn guard would then discard the genuine
        # COMPLETED with this id as "Already processed", leaving a paid
        # cycle never activated. Mirrors the Order path, which stores the
        # txn id only inside the COMPLETED/FAILED branches.
        db.commit()
