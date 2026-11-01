import logging
from datetime import datetime

from app.core.attachments import describe_attachments
from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.core.instagram_provider import InstagramProvider
from app.crud import instagram as crud_instagram
from app.db.models import ConversationState, InstagramAccount, InstagramCustomer, InstagramDirectMessage
from app.db.session import SessionLocal
from app.services import usage
from app.services.llm import LLMUnavailable, llm_service
from app.tasks.claim_verification import should_verify_payment_claim, verify_payment_claim
from app.tasks.webhook_processing import LLM_RETRY_COUNTDOWNS_SECONDS, _run_async, _send_with_retries

logger = logging.getLogger(__name__)


@celery_app.task
def process_inbound_dm(
    account_id: str, customer_id: str, dm_row_id: str, llm_attempt: int = 0, claimed_at: str | None = None
):
    """
    Generates and sends Amara's reply to an inbound Instagram DM, continuing
    whatever conversation is already on file for this customer. dm_row_id is
    the just-created InstagramDirectMessage.id for the inbound message.
    llm_attempt/claimed_at are only set on a Claude-outage retry (#38).
    """
    db = SessionLocal()
    try:
        return _run_async(_process(db, account_id, customer_id, dm_row_id, llm_attempt, claimed_at))
    finally:
        db.close()


async def _process(
    db, account_id: str, customer_id: str, dm_row_id: str, llm_attempt: int = 0, claimed_at: str | None = None
) -> str:
    account_uuid = coerce_uuid(account_id)
    customer_uuid = coerce_uuid(customer_id)
    dm_row_uuid = coerce_uuid(dm_row_id)
    if account_uuid is None or customer_uuid is None or dm_row_uuid is None:
        return "Account or customer not found."

    # Atomic claim: guards against two concurrent/duplicate executions of
    # this same inbound message both running the full LLM/send/order flow —
    # whether from webhook redelivery racing past handle_webhook's own
    # dm_already_processed check, or Celery's own at-least-once task
    # redelivery. Same conditional-UPDATE pattern as
    # claim_available_pool_number: the affected-row count tells us whether
    # we won the race, correct on both SQLite tests and Postgres prod.
    # Confirmed live: without this, a single inbound message produced two
    # separate PAID orders. See #33.
    # An outage retry (#38) re-claims by compare-and-swap on the exact
    # processed_at its own earlier attempt wrote, rather than releasing the
    # claim in between — releasing would let a webhook redelivery claim the
    # row too and reopen the double-order race.
    claim = db.query(InstagramDirectMessage).filter(InstagramDirectMessage.id == dm_row_uuid)
    if claimed_at is None:
        claim = claim.filter(InstagramDirectMessage.processed_at.is_(None))
    else:
        claim = claim.filter(InstagramDirectMessage.processed_at == datetime.fromisoformat(claimed_at))
    now = datetime.utcnow()
    claimed = claim.update({"processed_at": now})
    db.commit()
    if claimed == 0:
        return "Already processed; skipping duplicate dispatch."

    account = db.query(InstagramAccount).filter(InstagramAccount.id == account_uuid).first()
    customer = db.query(InstagramCustomer).filter(InstagramCustomer.id == customer_uuid).first()
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
        {"direction": m.direction, "body": m.body or describe_attachments(m.attachments)}
        for m in history_rows
        if m.id != dm_row_uuid
    ]
    latest = next((m for m in history_rows if m.id == dm_row_uuid), None)
    message_body = latest.body if latest else ""
    if not message_body and latest is not None:
        # Forward/share/sticker with no text — Amara acknowledges the
        # described event instead of seeing an empty message (#87).
        message_body = describe_attachments(latest.attachments)

    # Reply meter (#90): an exhausted plan gets the holding message
    # instead of a generated reply — and the holding message itself is
    # never metered, so this path skips record_reply below.
    quota_blocked = False
    # What the AI turn did (markers seen / order machinery ran) — filled
    # by generate_dm_response for the post-send payment-claim check (#116).
    turn_outcome: dict = {}
    holding = usage.check_reply_quota(db, account.owner) if account.owner else None
    if holding is not None:
        reply_text = holding
        quota_blocked = True
    else:
        try:
            reply_text = await llm_service.generate_dm_response(
                message_body=message_body or "",
                db=db,
                account=account,
                customer=customer,
                context=context,
                conversation_history=history,
                account_name=account.username or "the shop",
                retry_on_outage=llm_attempt < len(LLM_RETRY_COUNTDOWNS_SECONDS),
                turn_outcome=turn_outcome,
            )
        except LLMUnavailable as e:
            logger.warning("Claude unavailable for DM %s (attempt %s); retrying: %s", dm_row_uuid, llm_attempt, e)
            process_inbound_dm.apply_async(
                args=[account_id, customer_id, dm_row_id, llm_attempt + 1, now.isoformat()],
                countdown=LLM_RETRY_COUNTDOWNS_SECONDS[llm_attempt],
            )
            return "Retrying: LLM unavailable."
    if not reply_text:
        logger.warning("No usable DM reply generated for customer %s.", customer.id)
        return "No reply generated."

    provider = InstagramProvider(account.access_token)
    response = await _send_with_retries(
        lambda: provider.send_direct_message(account.ig_business_id, customer.ig_scoped_id, reply_text)
    )
    if "error" in response:
        logger.error("DM send to customer %s failed: %s", customer.id, response)
        # The customer never got this reply — the merchant has to step in (#38).
        try:
            await llm_service._notify_merchant_of_instagram_handoff(db, account, customer, message_body)
        except Exception as alert_err:
            logger.error("Failed to alert merchant of undelivered DM: %s", alert_err)
        return "Send failed."

    crud_instagram.create_direct_message(
        db,
        account_id=account.id,
        customer_id=customer.id,
        direction="outbound",
        body=reply_text,
        ig_message_id=response.get("message_id"),
    )
    if not quota_blocked:
        usage.record_reply(db, account.user_id, "instagram_dm", instagram_account_id=account.id)

    # Backstop for Amara promising a payment with no order behind it
    # (#116) — same shape as the WhatsApp hook: async verify after a
    # successful AI-reply send, quota holdings excluded.
    if not quota_blocked and should_verify_payment_claim(
        message_body,
        reply_text,
        markers_seen=turn_outcome.get("markers_seen", False),
        order_action=turn_outcome.get("order_action", False),
    ):
        try:
            verify_payment_claim.apply_async(
                args=[
                    "instagram",
                    str(account.id),
                    str(customer.id),
                    message_body,
                    reply_text,
                    turn_outcome.get("order_action", False),
                ]
            )
        except Exception as enqueue_err:
            logger.error(f"Failed to enqueue payment-claim verification: {enqueue_err}")

    return "Processed."
