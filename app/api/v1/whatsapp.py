from fastapi import APIRouter, Depends, HTTPException, Header, Request, Query, status
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
from app.core.webhook_parser import WebhookParser
from app.crud import message as crud_message
from app.tasks.webhook_processing import process_inbound_message
from app.schemas.whatsapp import (
    MessageRead,
    MessageSend,
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

    # Try matching by internal ID (if numeric and small enough) or phone_number_id
    is_numeric = str(account_id).isdigit()

    if is_numeric and len(str(account_id)) < 10:  # Safe bound for 32-bit int to prevent Postgres out-of-range errors
        account = query.filter(
            (WhatsAppAccount.id == int(account_id))
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


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    raw_body = await request.body()

    if settings.WHATSAPP_APP_SECRET:
        if not verify_meta_signature(raw_body, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        print(
            "WARNING: WHATSAPP_APP_SECRET is not configured; skipping webhook "
            "signature verification. Set it to prevent spoofed webhook payloads."
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

            crud_message.create_message(db, item, account_id)

            # 2. Routing logic — dispatched to a background task rather than
            # awaited here, so a slow Gemini call or WhatsApp send can't
            # hold up Meta's webhook ACK (which risks a retried delivery).
            # Merchant-vs-customer routing, per-customer locking, and rate
            # limiting all live inside the task (app/tasks/webhook_processing.py).
            if (
                item.get("direction") == "inbound"
                and account_ai_enabled
                and item.get("type") == "text"
            ):
                process_inbound_message.apply_async(
                    args=[account_id, item.get("from_phone"), item.get("body")]
                )

        # 3. Forward to callback if configured
        if account_callback_url:
            if not is_safe_external_url(account_callback_url):
                print(
                    f"Skipping forward to unsafe callback_url: {account_callback_url}"
                )
            else:
                async with httpx.AsyncClient() as client:
                    try:
                        await client.post(account_callback_url, json=item, timeout=5.0)
                    except Exception as e:
                        print(f"Failed to forward webhook to {account_callback_url}: {e}")

    return {"status": "ok"}


@router.get("/handoffs", response_model=list[PendingHandoffRead])
def get_pending_handoffs(
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    return crud_message.get_pending_handoffs_for_account(db, account.id)


@router.get("/messages", response_model=list[MessageRead])
def get_message_history(
    limit: int = 100,
    account: WhatsAppAccount = Depends(get_whatsapp_account),
    db: Session = Depends(get_db),
):
    return crud_message.get_messages_by_account(db, account.id, limit)


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
        response = await provider.send_text(msg.to, msg.text_body)
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
