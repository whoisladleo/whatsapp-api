import asyncio
import concurrent.futures
import logging
import uuid

import app.core.rate_limit as rate_limit_module
from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.core.provider import WhatsAppProvider
from app.core.rate_limit import check_and_increment
from app.core.security import normalize_phone
from app.crud import knowledge as crud_knowledge
from app.crud import message as crud_message
from app.db.models import WhatsAppAccount
from app.db.session import SessionLocal
from app.services.llm import llm_service
from app.services.merchant_handler import handle_merchant_message

logger = logging.getLogger(__name__)

# Guards concurrent processing of messages from the same customer on the
# same account, so two rapid inbound messages can't race each other into
# duplicate "first message" welcomes or interleaved replies. TTL is a safety
# net in case a worker crashes mid-processing and never releases the lock.
LOCK_TTL_SECONDS = 30
LOCK_RETRY_COUNTDOWN_SECONDS = 2
LOCK_MAX_RETRIES = 5  # ~10s of total contention before giving up

# Caps how many AI-generated replies a given customer can receive from a
# given account per hour, to bound Claude + WhatsApp-send cost from a single
# number. Does not apply to merchant-command processing, which is
# operational rather than abuse-prone the way inbound customer chat is.
AI_REPLY_RATE_LIMIT = 20
AI_REPLY_RATE_LIMIT_WINDOW_SECONDS = 60 * 60


def _run_async(coro):
    """
    Runs a coroutine to completion from this (sync) Celery task, whether or
    not an event loop is already running in the current thread. A real
    Celery worker has no loop running in its thread, so asyncio.run() alone
    would be enough there — but Celery's eager mode (task_always_eager,
    used by the test suite so apply_async runs the task inline instead of
    needing a broker) executes this task synchronously from within the
    async webhook request handler, i.e. from a thread that already has a
    loop running, where asyncio.run() raises. Running it in a fresh thread
    sidesteps that without needing to special-case test vs. production.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _lock_key(account_id: str, from_phone: str) -> str:
    return f"webhook_lock:{account_id}:{normalize_phone(from_phone)}"


def _acquire_lock(key: str) -> bool:
    try:
        return bool(
            rate_limit_module._redis_client.set(key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        )
    except Exception:
        # Fail open, matching check_and_increment's fail-open behavior — a
        # Redis outage shouldn't stop customers from getting replies.
        return True


def _release_lock(key: str) -> None:
    try:
        rate_limit_module._redis_client.delete(key)
    except Exception:
        pass


@celery_app.task
def process_inbound_message(
    account_id: str, from_phone: str, message_body: str, attempt: int = 0
):
    """
    Handles a single inbound customer/merchant text message: routes it to
    either the merchant command handler or the AI sales engine, sends the
    reply, and logs it. Moved off the webhook request path so a slow Claude
    call or WhatsApp send can't hold up Meta's webhook ACK and risk a
    retried (duplicate) delivery.

    Lock contention (see _acquire_lock) is handled by re-dispatching this
    same task via apply_async(countdown=...) with `attempt` incremented,
    rather than Celery's built-in self.retry(): retry() only actually
    re-queues through a real broker — under task_always_eager (used by the
    test suite so this can run without one) it just raises immediately
    instead of looping, which isn't useful to test or reason about. A plain
    recursive apply_async behaves the same way in both eager and
    real-worker execution: non-blocking in production (the delayed
    re-attempt is a genuinely separate broker message), and an immediate,
    deterministic, boundedly-recursive call under eager mode in tests.
    """
    db = SessionLocal()
    try:
        account_uuid = coerce_uuid(account_id)
        if account_uuid is None:
            return "Account not found."
        account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_uuid).first()
        if not account:
            return "Account not found."

        lock_key = _lock_key(account_id, from_phone)
        if not _acquire_lock(lock_key):
            if attempt >= LOCK_MAX_RETRIES:
                logger.warning(
                    f"Giving up on {lock_key} after {LOCK_MAX_RETRIES} retries "
                    "(another message for this customer never released the lock)."
                )
                return "Lock contention exhausted."

            process_inbound_message.apply_async(
                args=[account_id, from_phone, message_body, attempt + 1],
                countdown=LOCK_RETRY_COUNTDOWN_SECONDS,
            )
            return "Retrying due to lock contention."

        try:
            return _run_async(_process(db, account, from_phone, message_body))
        finally:
            _release_lock(lock_key)
    finally:
        db.close()


async def _process(db, account, from_phone, message_body) -> str:
    is_merchant = account.owner and normalize_phone(from_phone) == normalize_phone(
        account.owner.username
    )

    response_text = None

    if is_merchant:
        response_text = await handle_merchant_message(db, account, message_body, from_phone)
    elif llm_service:
        rate_limit_key = f"ai_reply:{account.id}:{normalize_phone(from_phone)}"
        if not check_and_increment(
            rate_limit_key, AI_REPLY_RATE_LIMIT, AI_REPLY_RATE_LIMIT_WINDOW_SECONDS
        ):
            logger.warning(
                f"AI reply rate limit exceeded for {from_phone} on account {account.id}; "
                "dropping this message without a reply."
            )
            return "Rate limited."

        knowledge_content = None
        if account.product_slug:
            kb = crud_knowledge.get_knowledge_base_by_slug(
                db, account.product_slug, account.user_id
            )
            if kb:
                knowledge_content = kb.content

        response_text = await llm_service.generate_response(
            message_body=message_body,
            product_slug=account.product_slug,
            knowledge_content=knowledge_content,
            system_prompt=account.system_prompt,
            from_phone=from_phone,
            db=db,
            account=account,
        )

    if response_text:
        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        resp = await provider.send_text(from_phone, response_text)

        if resp and "messages" in resp:
            msg_id = resp["messages"][0].get("id")
            status = "sent"
        else:
            msg_id = f"failed_{uuid.uuid4().hex[:8]}"
            status = "failed"
            logger.error(f"WhatsApp API Error: {resp}")

        crud_message.create_message(
            db,
            {
                "whatsapp_message_id": msg_id,
                "to_phone": from_phone,
                "type": "text",
                "body": response_text,
                "direction": "outbound",
                "status": status,
                "raw": resp if resp else {},
            },
            account.id,
        )

    return "Success"
