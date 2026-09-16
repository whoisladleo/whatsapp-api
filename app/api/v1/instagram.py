from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.instagram_webhook_parser import InstagramWebhookParser
from app.core.security import verify_meta_signature
from app.crud import instagram as crud_instagram
from app.db.session import get_db
from app.tasks.instagram_comment_processing import process_inbound_comment

router = APIRouter()


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    raw_body = await request.body()

    # "Taskflow - IG" is a separate Meta App from the WhatsApp one, with its
    # own app secret — not WHATSAPP_APP_SECRET.
    if settings.INSTAGRAM_APP_SECRET:
        if not verify_meta_signature(
            raw_body, x_hub_signature_256, app_secret=settings.INSTAGRAM_APP_SECRET
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        print(
            "WARNING: INSTAGRAM_APP_SECRET is not configured; skipping webhook "
            "signature verification. Set it to prevent spoofed webhook payloads."
        )

    data = await request.json()
    parsed_comments = InstagramWebhookParser.parse_inbound(data)

    for item in parsed_comments:
        account = crud_instagram.get_account_by_ig_business_id(
            db, item["ig_business_id"]
        )
        if not account:
            continue

        if crud_instagram.comment_already_processed(db, item["comment_id"]):
            continue

        comment = crud_instagram.create_comment(db, account.id, item)

        if account.ai_enabled:
            process_inbound_comment.apply_async(args=[account.id, comment.id])

    return {"status": "ok"}
