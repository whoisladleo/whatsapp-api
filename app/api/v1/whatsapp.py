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
import httpx
from app.core.webhook_parser import WebhookParser
from app.crud import message as crud_message
from app.crud import whatsapp as crud_account
from app.services.llm import llm_service
from app.schemas.whatsapp import (
    MessageRead,
    MessageSend,
    WhatsAppAccountCreate,
    WhatsAppAccountInDB,
    WhatsAppAccountUpdate,
)

router = APIRouter()

security_scheme = HTTPBearer()


async def get_current_user(
    db: Session = Depends(get_db),
    token: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> User:
    try:
        payload = jwt.decode(
            token.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def get_whatsapp_account(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    whatsapp_account_id: Optional[str] = Header(None, alias="X-WhatsApp-Account-ID"),
    x_account_id: Optional[str] = Header(None, alias="X-Account-ID"),
    x_account_id_query: Optional[str] = Query(None, alias="X-Account-ID"),
) -> WhatsAppAccount:
    account_id = whatsapp_account_id or x_account_id or x_account_id_query
    query = db.query(WhatsAppAccount).filter(WhatsAppAccount.user_id == current_user.id)

    if account_id:
        # Try matching by internal ID (if numeric) or phone_number_id
        is_numeric = str(account_id).isdigit()
        if is_numeric:
            account = query.filter(
                (WhatsAppAccount.id == int(account_id))
                | (WhatsAppAccount.phone_number_id == str(account_id))
            ).first()
        else:
            account = query.filter(
                WhatsAppAccount.phone_number_id == str(account_id)
            ).first()
    else:
        # Default to the first account if no specific ID is provided
        account = query.first()

    if not account:
        raise HTTPException(status_code=404, detail="WhatsApp account not found")
    return account


@router.post("/accounts", response_model=WhatsAppAccountInDB)
def create_account(
    account_in: WhatsAppAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = crud_account.create_whatsapp_account(db, account_in, current_user.id)
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
async def handle_webhook(request: Request, db: Session = Depends(get_db)):
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
            print(f"DEBUG: Processing inbound message from {item.get('from_phone')}: {item.get('body')}")
            crud_message.create_message(db, item, account.id)

        # 2. AI Response Logic
        if (
            item.get("direction") == "inbound"
            and account.ai_enabled
            and llm_service
            and item.get("type") == "text"
        ):
            print(f"DEBUG: Generating AI response for account {account.name}...")
            response_text = await llm_service.generate_response(
                message_body=item.get("body"),
                product_slug=account.product_slug,
                system_prompt=account.system_prompt,
            )
            print(f"DEBUG: AI Response: {response_text}")

            # Send the response back
            provider = WhatsAppProvider(account.access_token, account.phone_number_id)
            resp = await provider.send_text(item["from_phone"], response_text)
            
            if resp and "messages" in resp:
                print(f"DEBUG: AI message sent successfully. ID: {resp['messages'][0].get('id')}")
                # Log outbound AI message
                msg_id = resp["messages"][0].get("id")
                crud_message.create_message(
                    db,
                    {
                        "whatsapp_message_id": msg_id,
                        "to_phone": item["from_phone"],
                        "type": "text",
                        "body": response_text,
                        "direction": "outbound",
                        "status": "sent",
                        "raw": resp,
                    },
                    account.id,
                )
            else:
                print(f"ERROR: Failed to send AI response. Meta Response: {resp}")

        # 3. Forward to callback if configured
        if account.callback_url:
            async with httpx.AsyncClient() as client:
                try:
                    await client.post(account.callback_url, json=item, timeout=5.0)
                except Exception as e:
                    print(f"Failed to forward webhook to {account.callback_url}: {e}")

    return {"status": "ok"}


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
                "body": msg.text_body or msg.template_name or msg.media_id,
                "direction": "outbound",
                "status": "sent",
                "raw": response,
            },
            account.id,
        )

    return response
