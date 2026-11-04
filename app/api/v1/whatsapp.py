import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, Request, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import Optional
import jwt
from app.db.session import get_db
from app.db.models import Message, User, WhatsAppAccount
from app.core.config import settings
from app.core.provider import WhatsAppProvider
from app.core.security import is_safe_external_url, verify_meta_signature
import httpx
from app.core.attachments import describe_attachments
from app.core.webhook_parser import WebhookParser
from app.services import resume as resume_service
from app.services.handoff_flow import normalize_button_id
from app.crud import message as crud_message
from app.tasks.webhook_processing import apply_async_or_rollback, process_inbound_message
from app.tasks.location_capture import capture_customer_location
from app.schemas.whatsapp import (
    ConversationSummary,
    MessageRead,
    MessageSend,
    MessageStats,
    PendingHandoffRead,
)

router = APIRouter()

security_scheme = HTTPBearer()


from app.api.deps import get_current_user


async def get_whatsapp_account(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    whatsapp_account_id: Optional[str] = Header(None, alias="X-WhatsApp-Account-ID"),
    x_account_id: Optional[str] = Header(None, alias="X-Account-ID"),
    x_account_id_query: Optional[str] = Query(None, alias="X-Account-ID"),
) -> WhatsAppAccount:
    account_id = whatsapp_account_id or x_account_id or x_account_id_query

    if not account_id:
        raise HTTPException(status_code=400, detail="WhatsApp Account ID is required via header (X-WhatsApp-Account-ID) or query param")

    query = db.query(WhatsAppAccount)

    # account_id is either our internal UUID id or Meta's phone_number_id
    # string — try parsing as a UUID first, and only match against it when
    # that succeeds (a phone_number_id never happens to be a valid UUID).
    try:
        internal_id = uuid.UUID(str(account_id))
    except ValueError:
        internal_id = None

    if internal_id is not None:
        account = query.filter(
            (WhatsAppAccount.id == internal_id)
            | (WhatsAppAccount.phone_number_id == str(account_id))
        ).first()
    else:
        account = query.filter(
            WhatsAppAccount.phone_number_id == str(account_id)
        ).first()

    # Scope to the authenticated user's own accounts; 404 (not 403) to avoid
    # leaking whether an account id/phone_number_id exists for another user.
    if not account or account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    return account


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge)
    
    raise HTTPException(status_code=403, detail="Verification failed")


async def _forward_to_callback(callback_url: str, payload: dict) -> None:
    """Best-effort merchant forward; runs as a BackgroundTasks task after the
    Meta ACK, so a slow merchant endpoint can't delay it. Failures are
    logged, never raised."""
    # Re-checked here, immediately before the POST — the enqueue-time check
    # in handle_webhook runs before this task executes, and DNS can
    # re-resolve to a different address in between (SSRF via rebinding).
    if not is_safe_external_url(callback_url):
        print(f"Skipping forward to unsafe callback_url: {callback_url}")
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(callback_url, json=payload, timeout=5.0)
    except Exception as e:
        print(f"Failed to forward webhook to {callback_url}: {e}")


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    raw_body = await request.body()

    if settings.WHATSAPP_APP_SECRET:
        if not verify_meta_signature(raw_body, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    elif not settings.ALLOW_UNSIGNED_WEBHOOKS:
        raise HTTPException(status_code=403, detail="Webhook signature verification is not configured")
    else:
        print(
            "WARNING: WHATSAPP_APP_SECRET is not configured and "
            "ALLOW_UNSIGNED_WEBHOOKS=true; accepting unsigned webhook payloads."
        )

    data = await request.json()
    parsed_items = WebhookParser.parse_inbound(data)

    for item in parsed_items:
        # Find account by phone_number_id
        phone_id = item.get("phone_number_id")
        account = (
            db.query(WhatsAppAccount)
            .filter(WhatsAppAccount.phone_number_id == phone_id)
            .first()
        )

        if not account:
            continue

        # Captured up front rather than read off `account` further down:
        # dispatching process_inbound_message can (in tests, where Celery
        # runs it eagerly/inline and it shares this request's DB session)
        # close that session before this loop body finishes, which would
        # detach `account` and make later attribute access raise
        # DetachedInstanceError. In production the task uses its own
        # independent session, so this wouldn't happen either way — but
        # capturing plain values here is correct and cheap regardless.
        account_id = account.id
        account_ai_enabled = account.ai_enabled
        account_callback_url = account.callback_url
        account_is_platform = account.is_platform_account

        # 1. Update status, or create the inbound message (deduping Meta
        # retries of a delivery we've already processed — skip re-creating
        # it and re-triggering AI/merchant processing/callback forwarding
        # for it entirely).
        if item.get("event") == "status_update":
            crud_message.update_message_status(
                db, item["whatsapp_message_id"], item["status"]
            )
        else:
            whatsapp_message_id = item.get("whatsapp_message_id")
            if whatsapp_message_id:
                already_processed = (
                    db.query(Message)
                    .filter(Message.whatsapp_message_id == whatsapp_message_id)
                    .first()
                    is not None
                )
                if already_processed:
                    continue

            stored = crud_message.create_message(db, item, account_id)

            # 2. Routing logic — dispatched to a background task rather than
            # awaited here, so a slow Claude call or WhatsApp send can't
            # hold up Meta's webhook ACK (which risks a retried delivery).
            # Merchant-vs-customer routing, per-customer locking, and rate
            # limiting all live inside the task (app/tasks/webhook_processing.py).
            if (
                item.get("direction") == "inbound"
                and account_ai_enabled
                and item.get("type") in ("text", "image", "video", "audio", "document", "sticker", "contacts")
            ):
                # Media without a caption carries no text — describe the
                # attachment so Amara acknowledges it instead of seeing "".
                message_body = item.get("body") or describe_attachments(item.get("attachments"))
                apply_async_or_rollback(
                    process_inbound_message,
                    [str(account_id), item.get("from_phone"), message_body],
                    db, Message, stored.id,
                )
            elif (
                item.get("direction") == "inbound"
                and (
                    (
                        item.get("type") == "interactive"
                        and (item.get("interactive_id") or item.get("body"))
                    )
                    # Template quick-reply taps may also arrive as type
                    # "button" (button.text = the title, no payload id) —
                    # same tap, different envelope. Route them identically
                    # instead of storing them silent.
                    or (item.get("type") == "button" and item.get("body"))
                )
                and (
                    account_is_platform
                    or (
                        normalize_button_id(item.get("interactive_id"), item.get("body")) or ""
                    ).startswith("handoff_")
                )
            ):
                # Handoff template button tap — not gated on ai_enabled,
                # merchant tooling stays live even when AI replies are off.
                # Every platform-chat tap dispatches (even unrecognized
                # ones — handle_button answers those loudly) so a payload
                # shape Meta drifts never strands a merchant in silence
                # again (#96). Business-number taps still need a handoff id.
                apply_async_or_rollback(
                    process_inbound_message,
                    [
                        str(account_id),
                        item.get("from_phone"),
                        item.get("body") or "",
                        0,
                        0,
                        normalize_button_id(item.get("interactive_id"), item.get("body")),
                        item.get("context_message_id"),
                    ],
                    db, Message, stored.id,
                )
            elif (
                item.get("direction") == "inbound"
                and item.get("type") == "location"
                and item.get("location")
            ):
                # Not gated on account_ai_enabled — capturing a delivery
                # location is a fulfillment signal, not an AI/merchant
                # conversation action, so it's accepted regardless.
                loc = item["location"]
                apply_async_or_rollback(
                    capture_customer_location,
                    [
                        str(account_id),
                        item.get("from_phone"),
                        loc.get("latitude"),
                        loc.get("longitude"),
                        loc.get("name"),
                        loc.get("address"),
                    ],
                    db, Message, stored.id,
                )

        # 3. Forward to callback if configured — scheduled to run after
        # the response, so a slow merchant endpoint can't hold up Meta's
        # ACK (which risks a retried delivery). Chose BackgroundTasks over
        # a Celery task: this forward is best-effort and must never 500
        # the webhook the way apply_async_or_rollback does on failure.
        if account_callback_url:
            if not is_safe_external_url(account_callback_url):
                print(
                    f"Skipping forward to unsafe callback_url: {account_callback_url}"
                )
            else:
                background_tasks.add_task(_forward_to_callback, account_callback_url, item)

    # Traffic-triggered daily sweep (no Celery Beat in this stack): at most
    # one webhook per rolling 24h actually queues it; never raises.
    from app.tasks.subscription_nudges import maybe_dispatch_daily_sweep

    maybe_dispatch_daily_sweep()

    return {"status": "ok"}


@router.get("/conversations", response_model=list[ConversationSummary])
def get_conversations(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    exclude_phone = account.owner.username if account.owner else None
    return crud_message.get_conversations_for_account(
        db, account.id, exclude_phone=exclude_phone, limit=limit, offset=offset
    )


@router.get("/stats", response_model=MessageStats)
def get_message_stats(
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    exclude_phone = account.owner.username if account.owner else None
    return crud_message.get_message_stats_for_account(db, account.id, exclude_phone=exclude_phone)


@router.get("/handoffs", response_model=list[PendingHandoffRead])
def get_pending_handoffs(
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    return crud_message.get_pending_handoffs_for_account(db, account.id)


@router.post("/handoffs/{phone}/resume")
def resume_handoff(
    phone: str,
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    """HTTP equivalent of the WhatsApp `/resume` merchant command, for a dashboard inbox."""
    try:
        crud_message.resume_conversation_for_account(db, account.id, phone)
    except crud_message.ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except crud_message.ConversationNotInHandoffError as e:
        raise HTTPException(status_code=400, detail=str(e))
    verdict = resume_service.verdict_for_whatsapp(db, account.id, phone)
    queued = resume_service.dispatch_resume_reply(verdict)
    return {
        "status": "resumed",
        "phone": phone,
        "resume_reply": "queued" if queued else f"skipped_{verdict.reason}",
    }


@router.get("/messages", response_model=list[MessageRead])
def get_message_history(
    limit: int = Query(default=100, ge=1, le=500),
    phone: Optional[str] = None,
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    messages = crud_message.get_messages_by_account(db, account.id, limit, customer_phone=phone)
    # Pre-#87 rows stored only raw_data — derive the same normalized shape
    # on read so old media renders instead of showing a bare media id.
    for message in messages:
        if message.attachments is None and isinstance(message.raw_data, dict):
            derived = WebhookParser.extract_attachments(message.type, message.raw_data)
            if derived:
                message.attachments = derived
                # Pre-#87 bodies for these types are always the bare media
                # id — the attachment placeholder replaces them. Read-path
                # only; nothing is committed.
                message.body = ""
    return messages


@router.post("/messages/send")
async def send_message(
    msg: MessageSend,
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    response = None

    if msg.type == "template":
        response = await provider.send_template(msg.to, msg.template_name)
    elif msg.type == "text":
        response = await provider.send_text(
            msg.to, msg.text_body, context_message_id=msg.context_message_id
        )
    elif msg.type in ["image", "document"]:
        response = await provider.send_media(
            msg.to, msg.media_id, msg.type, msg.caption
        )

    if response and "messages" in response:
        # Log the outbound message
        msg_id = response["messages"][0].get("id")
        crud_message.create_message(
            db,
            {
                "whatsapp_message_id": msg_id,
                "to_phone": msg.to,
                "type": msg.type,
                "body": (
                    msg.template_name if msg.type == "template" else
                    msg.text_body if msg.type == "text" else
                    msg.media_id
                ),
                "direction": "outbound",
                "status": "sent",
                "raw": response,
            },
            account.id,
        )
        return response

    # WhatsAppProvider always returns response.json() regardless of HTTP
    # status, so a Meta-side rejection lands here as {"error": {...}} —
    # without this check it would silently return 200, and the dashboard's
    # reply UI would show "sent" for a message that never left the server.
    error_detail = (
        response.get("error", {}).get("message", "Failed to send message")
        if response else "Failed to send message"
    )
    # 422, not 502: this is Meta's API rejecting a well-formed request, not
    # a gateway/infra failure — confirmed live (see Instagram's identical
    # send_message) that 502 gets its body swallowed and replaced by
    # whatever reverse proxy sits in front of this API, so error_detail
    # above never actually reaches the dashboard despite being set here.
    raise HTTPException(status_code=422, detail=error_detail)
