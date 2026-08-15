from fastapi import APIRouter, Depends, HTTPException, Header, Request, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import Optional
import jwt
from app.db.session import get_db
from app.db.models import User, WhatsAppAccount
from app.core.config import settings
from app.core.provider import WhatsAppProvider
from app.core.security import is_safe_external_url, verify_meta_signature
import httpx
from app.core.webhook_parser import WebhookParser
from app.crud import message as crud_message
from app.crud import whatsapp as crud_account
from app.services.llm import llm_service
from app.crud import knowledge as crud_knowledge
from app.services.merchant_handler import handle_merchant_message
from app.schemas.whatsapp import (
    MessageRead,
    MessageSend,
    PendingHandoffRead,
    WhatsAppAccountCreate,
    WhatsAppAccountInDB,
    WhatsAppAccountUpdate,
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


@router.post("/accounts", response_model=WhatsAppAccountInDB)
def create_account(
    account_in: WhatsAppAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        account = crud_account.create_whatsapp_account(db, account_in, current_user.id)
    except crud_account.PartialWhatsAppCredentialsError:
        raise HTTPException(
            status_code=400,
            detail="Provide both phone_number_id and access_token, or omit both to auto-assign a number from the pool.",
        )
    except crud_account.PhonePoolExhaustedError:
        raise HTTPException(
            status_code=409,
            detail="No WhatsApp numbers are currently available in the pool. Try again shortly, or provide your own phone_number_id/access_token.",
        )
    if not account:
        raise HTTPException(
            status_code=400, detail="WhatsApp account with this ID already exists"
        )
    return account


@router.get("/accounts", response_model=list[WhatsAppAccountInDB])
def list_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
):
    return crud_account.get_multi_by_user(db, current_user.id, skip=skip, limit=limit)


@router.get("/accounts/{account_id}", response_model=WhatsAppAccountInDB)
def get_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.id == account_id, WhatsAppAccount.user_id == current_user.id
        )
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    return account


@router.patch("/accounts/{account_id}", response_model=WhatsAppAccountInDB)
def update_account(
    account_id: int,
    account_in: WhatsAppAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_account = (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.id == account_id, WhatsAppAccount.user_id == current_user.id
        )
        .first()
    )
    if not db_account:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    return crud_account.update_whatsapp_account(db, db_account, account_in)


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_account = (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.id == account_id, WhatsAppAccount.user_id == current_user.id
        )
        .first()
    )
    if not db_account:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    crud_account.delete_whatsapp_account(db, db_account)
    return {"status": "deleted"}


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

        # 1. Update status or Create new message
        if item.get("event") == "status_update":
            crud_message.update_message_status(
                db, item["whatsapp_message_id"], item["status"]
            )
        else:
            crud_message.create_message(db, item, account.id)

        # 2. Routing Logic
        if (
            item.get("direction") == "inbound"
            and account.ai_enabled
            and item.get("type") == "text"
        ):
            from_phone = item.get("from_phone")
            message_body = item.get("body")
            
            # Simple merchant check: is the from_phone the owner's username?
            # Assuming owner username is their phone number for the MVP.
            is_merchant = (from_phone == account.owner.username)
            
            response_text = None
            
            if is_merchant:
                # Route to Merchant Catalog Management
                response_text = await handle_merchant_message(db, account, message_body, from_phone)
            elif llm_service:
                # Route to Customer AI Sales Engine
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
                    account=account
                )

            # Send the response back
            if response_text:
                provider = WhatsAppProvider(account.access_token, account.phone_number_id)
                resp = await provider.send_text(from_phone, response_text)
                
                if resp and "messages" in resp:
                    # Meta API succeeded
                    msg_id = resp["messages"][0].get("id")
                    status = "sent"
                else:
                    # Meta API failed, but we still want to log the AI's response in our DB
                    import uuid
                    msg_id = f"failed_{uuid.uuid4().hex[:8]}"
                    status = "failed"
                    print(f"WhatsApp API Error: {resp}")

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


        # 3. Forward to callback if configured
        if account.callback_url:
            if not is_safe_external_url(account.callback_url):
                print(
                    f"Skipping forward to unsafe callback_url: {account.callback_url}"
                )
            else:
                async with httpx.AsyncClient() as client:
                    try:
                        await client.post(account.callback_url, json=item, timeout=5.0)
                    except Exception as e:
                        print(f"Failed to forward webhook to {account.callback_url}: {e}")

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
