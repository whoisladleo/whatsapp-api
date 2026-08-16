import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.db.models import ConversationContext, WhatsAppAccount
from app.crud import message as crud_message

logger = logging.getLogger(__name__)

# Escalating spacing between nudges, not back-to-back — 1h, then +5h
# (cumulative 6h), +18h (cumulative 1 day), +2 days (cumulative 3 days),
# +4 days (cumulative 7 days / 1 week). Five nudges total, spanning a full
# week; the cascade stops after the fifth.
QUOTE_NUDGE_INTERVALS_SECONDS = [3600, 18000, 64800, 172800, 345600]
MAX_QUOTE_NUDGES = len(QUOTE_NUDGE_INTERVALS_SECONDS)


def _send_sync(provider, to, body):
    """Celery tasks run outside an event loop; see order_reminders._send_sync."""
    return asyncio.run(provider.send_text(to, body))


@celery_app.task
def send_quote_nudge(context_id: int, account_id: int, scheduled_quote_at_iso: str, nudge_number: int):
    """
    One leg of the "still interested?" follow-up cascade for a customer who
    was quoted a price and went quiet. Scheduled once at quote time (see
    LLMService, which schedules nudge_number=1), and reschedules itself for
    the next nudge on success — a self-rescheduling chain, same pattern as
    webhook_processing.py's lock-retry logic, rather than several
    independently-scheduled tasks.

    Staleness check: scheduled_quote_at_iso and nudge_number must match the
    context's *current* pending quote exactly. Celery has no cheap way to
    cancel an already-scheduled task, so if the quote was cleared or
    replaced by a newer one in the meantime (see generate_response's
    proactive clearing), this just no-ops instead of sending a stale nudge.
    """
    logger.info(f"Executing quote nudge {nudge_number} for context {context_id}")
    db = SessionLocal()
    try:
        context = db.query(ConversationContext).filter(ConversationContext.id == context_id).first()
        if not context or not context.pending_quote_at:
            return "No pending quote for this context."

        if context.pending_quote_at.isoformat() != scheduled_quote_at_iso:
            return "Quote was superseded by a newer one; skipping stale nudge."

        if (context.quote_nudge_count or 0) != nudge_number - 1:
            return "Nudge count has moved on; skipping stale nudge."

        customer = context.customer
        if not customer:
            return "No customer linked to this context."

        account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_id).first()
        if not account:
            return "Account not found."

        from app.core.provider import WhatsAppProvider

        product_name = context.current_product_intent or "that item"
        body = f"Still interested in the {product_name}? Happy to hold it for you if you let me know."

        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        try:
            resp = _send_sync(provider, customer.phone_number, body)
        except Exception as e:
            logger.error(f"Quote nudge send failed for context {context_id}: {e}")
            resp = None

        crud_message.create_message(
            db,
            {
                "to_phone": customer.phone_number,
                "type": "text",
                "body": body,
                "direction": "outbound",
                "status": "sent" if resp else "failed",
                "raw": resp or {},
            },
            account.id,
        )

        context.quote_nudge_count = nudge_number
        db.commit()

        if nudge_number < MAX_QUOTE_NUDGES:
            try:
                send_quote_nudge.apply_async(
                    args=[context_id, account_id, scheduled_quote_at_iso, nudge_number + 1],
                    countdown=QUOTE_NUDGE_INTERVALS_SECONDS[nudge_number],
                )
            except Exception as sched_err:
                logger.error(f"Failed to schedule next quote nudge for context {context_id}: {sched_err}")

        return "Success"
    finally:
        db.close()
