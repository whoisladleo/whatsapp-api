import logging

from app.core.celery_app import celery_app
from app.core.instagram_provider import InstagramProvider
from app.crud import instagram as crud_instagram
from app.db.models import ConversationState, InstagramAccount, InstagramCustomer
from app.db.session import SessionLocal
from app.services.llm import llm_service
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def process_inbound_dm(account_id: int, customer_id: int, dm_row_id: int):
    """
    Generates and sends Amara's reply to an inbound Instagram DM, continuing
    whatever conversation is already on file for this customer. dm_row_id is
    the just-created InstagramDirectMessage.id for the inbound message.
    """
    db = SessionLocal()
    try:
        return _run_async(_process(db, account_id, customer_id, dm_row_id))
    finally:
        db.close()


async def _process(db, account_id: int, customer_id: int, dm_row_id: int) -> str:
    account = db.query(InstagramAccount).filter(InstagramAccount.id == account_id).first()
    customer = db.query(InstagramCustomer).filter(InstagramCustomer.id == customer_id).first()
    if not account or not customer:
        return "Account or customer not found."

    context = crud_instagram.get_or_create_conversation(db, customer.id)
    if context.state == ConversationState.HUMAN_HANDOFF:
        # A human is already handling this conversation — stay silent.
        return "Conversation is in human handoff; not replying."

    if llm_service is None:
        logger.warning("llm_service unavailable; skipping DM for customer %s.", customer.id)
        return "LLM unavailable."

    history_rows = crud_instagram.get_dm_history(db, customer.id, limit=20)
    # The just-created inbound row is already in history_rows (last item) —
    # the model prompt separates "recent conversation" from "customer just
    # sent" for readability, so exclude that final inbound row from history.
    history = [
        {"direction": m.direction, "body": m.body}
        for m in history_rows
        if m.id != dm_row_id
    ]
    latest = next((m for m in history_rows if m.id == dm_row_id), None)
    message_body = latest.body if latest else ""

    reply_text = await llm_service.generate_dm_response(
        message_body=message_body or "",
        db=db,
        account=account,
        customer=customer,
        context=context,
        conversation_history=history,
        account_name=account.username or "the shop",
    )
    if not reply_text:
        logger.warning("No usable DM reply generated for customer %s.", customer.id)
        return "No reply generated."

    provider = InstagramProvider(account.access_token)
    response = await provider.send_direct_message(
        account.ig_business_id, customer.ig_scoped_id, reply_text
    )
    if "error" in response:
        logger.error("DM send to customer %s failed: %s", customer.id, response)
        return "Send failed."

    crud_instagram.create_direct_message(
        db,
        account_id=account.id,
        customer_id=customer.id,
        direction="outbound",
        body=reply_text,
        ig_message_id=response.get("message_id"),
    )

    return "Processed."
