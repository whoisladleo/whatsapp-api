import logging
from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.db.models import ConversationContext, ConversationState

logger = logging.getLogger(__name__)

# Single follow-up nudge, sent once, this long after the initial handoff alert.
HANDOFF_REMINDER_DELAY_SECONDS = 15 * 60


@celery_app.task
def send_handoff_reminder(context_id: int):
    """
    One-shot follow-up: if a conversation is still sitting in HUMAN_HANDOFF
    HANDOFF_REMINDER_DELAY_SECONDS after the initial alert, re-notify the
    merchant. Scheduled once at handoff time (see llm.py) and does not
    reschedule itself, to avoid nagging the merchant indefinitely.
    """
    logger.info(f"Executing handoff reminder for context {context_id}")
    db = SessionLocal()
    try:
        context = db.query(ConversationContext).filter(ConversationContext.id == context_id).first()
        if not context or context.state != ConversationState.HUMAN_HANDOFF:
            return "Context not found or no longer awaiting handoff."

        return _notify_merchant(db, context)
    finally:
        db.close()


def _notify_merchant(db, context) -> str:
    from app.db.models import Message, WhatsAppAccount
    from app.core.provider import WhatsAppProvider
    from app.crud import message as crud_message

    customer = context.customer
    if not customer:
        return "No customer linked to this context."

    # Find the WhatsApp account this customer last messaged, so we know which
    # merchant/account to alert and which credentials to send with. There is
    # no direct customer->account link in the schema, so infer it from the
    # most recent inbound message from this customer.
    last_inbound = (
        db.query(Message)
        .filter(Message.from_phone == customer.phone_number, Message.direction == "inbound")
        .order_by(Message.timestamp.desc())
        .first()
    )
    if not last_inbound or not last_inbound.account_id:
        return "No account found for this customer."

    account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == last_inbound.account_id).first()
    if not account or not account.owner:
        return "No merchant to notify."

    merchant_phone = account.owner.username
    alert_message = (
        f"⏰ *REMINDER: Handoff still pending* ⏰\n"
        f"A customer at {customer.phone_number} is still waiting for a human agent."
    )

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = provider_send_text_sync(provider, merchant_phone, alert_message)
    except Exception as e:
        logger.error(f"Failed to send handoff reminder to merchant: {e}")
        resp = None

    crud_message.create_message(
        db,
        {
            "to_phone": merchant_phone,
            "type": "text",
            "body": alert_message,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )
    return "Success"


def provider_send_text_sync(provider, to, body):
    """
    Celery tasks run outside an event loop, but WhatsAppProvider.send_text is
    async. Run it to completion here via asyncio.run.
    """
    import asyncio
    return asyncio.run(provider.send_text(to, body))
