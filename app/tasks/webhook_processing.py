import asyncio
import concurrent.futures
import logging
import uuid

import httpx

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
from app.services.llm import LLMUnavailable, llm_service
from app.services import usage
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

# A Claude outage that outlasts the SDK's own retries re-runs the whole turn
# after each of these delays (~2 min total) before the customer gets the
# outage reply and the merchant is alerted (#38). Safe because
# LLMUnavailable is raised before any order, send or state change.
LLM_RETRY_COUNTDOWNS_SECONDS = (15, 45, 60)

# Meta send retries happen in-process, after the reply (and any order) is
# final, so the LLM turn never re-runs. Kept short to stay well inside
# LOCK_TTL_SECONDS.
SEND_RETRY_DELAYS_SECONDS = (1, 3)


def apply_async_or_rollback(task, args, db, model, row_id):
    """
    Queues a webhook-triggered task, deleting the just-stored row when the
    broker is unreachable (#78). Without this, the row stays stored while
    the webhook 500s, and Meta's retry hits the already-processed check and
    no-ops — the message is dropped forever with no reply (observed live).
    With it, the retry finds no row and replays fully. The original error
    is re-raised so the webhook still 500s and Meta does retry; a failing
    rollback is logged, never masking it.
    """
    try:
        task.apply_async(args=args)
    except Exception:
        logger.exception("Task dispatch failed; rolling back stored row")
        try:
            db.query(model).filter(model.id == row_id).delete()
            db.commit()
        except Exception:
            logger.exception("Dispatch rollback failed")
        raise


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


def _is_transient_send_failure(resp) -> bool:
    error = (resp or {}).get("error") or {}
    # Meta codes 1/2: unknown error / service temporarily unavailable.
    return bool(error.get("is_transient")) or error.get("code") in (1, 2)


async def _send_with_retries(send):
    """
    Calls send() (a zero-arg coroutine factory returning Meta's JSON) and
    retries only transient failures. A network exception becomes an error
    dict instead of crashing the task, so the caller still logs the
    outbound row and can alert the merchant.
    """
    for delay in (*SEND_RETRY_DELAYS_SECONDS, None):
        try:
            resp = await send()
        except httpx.HTTPError as e:
            resp = {"error": {"message": str(e), "is_transient": True}}
        if delay is None or not _is_transient_send_failure(resp):
            return resp
        await asyncio.sleep(delay)


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
    account_id: str, from_phone: str, message_body: str, attempt: int = 0, llm_attempt: int = 0
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
    A Claude outage (LLMUnavailable) re-dispatches the same way, counted
    separately in `llm_attempt` (see LLM_RETRY_COUNTDOWNS_SECONDS).
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
                args=[account_id, from_phone, message_body, attempt + 1, llm_attempt],
                countdown=LOCK_RETRY_COUNTDOWN_SECONDS,
            )
            return "Retrying due to lock contention."

        try:
            return _run_async(_process(db, account, from_phone, message_body, llm_attempt=llm_attempt))
        except LLMUnavailable as e:
            outage = e
        finally:
            _release_lock(lock_key)

        # Dispatched only after the lock is released, so the retry can take it.
        logger.warning(f"Claude unavailable for {lock_key} (attempt {llm_attempt}); retrying: {outage}")
        process_inbound_message.apply_async(
            args=[account_id, from_phone, message_body, 0, llm_attempt + 1],
            countdown=LLM_RETRY_COUNTDOWNS_SECONDS[llm_attempt],
        )
        return "Retrying: LLM unavailable."
    finally:
        db.close()


async def _process(db, account, from_phone, message_body, llm_attempt: int = 0) -> str:
    is_merchant = account.owner and normalize_phone(from_phone) == normalize_phone(
        account.owner.username
    )

    response_text = None

    if is_merchant:
        response_text = await handle_merchant_message(db, account, message_body, from_phone)
    elif llm_service:
        rate_limit_key = f"ai_reply:{account.id}:{normalize_phone(from_phone)}"
        # An outage retry is the same message, already counted once.
        if llm_attempt == 0 and not check_and_increment(
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
            retry_on_outage=llm_attempt < len(LLM_RETRY_COUNTDOWNS_SECONDS),
        )

    if response_text:
        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        resp = await _send_with_retries(lambda: provider.send_text(from_phone, response_text))

        if resp and "messages" in resp:
            msg_id = resp["messages"][0].get("id")
            status = "sent"
            if not is_merchant:
                usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)
        else:
            msg_id = f"failed_{uuid.uuid4().hex[:8]}"
            status = "failed"
            logger.error(f"WhatsApp API Error: {resp}")
            if not is_merchant and account.owner:
                # The customer never got this reply — the merchant has to step in (#38).
                try:
                    await llm_service._send_platform_alert_to_merchant(db, account.owner, from_phone, message_body)
                except Exception as alert_err:
                    logger.error(f"Failed to alert merchant of undelivered reply: {alert_err}")

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
