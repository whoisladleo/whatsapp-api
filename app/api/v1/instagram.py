import logging
from datetime import timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import security
from app.core.config import settings
from app.core.instagram_webhook_parser import InstagramWebhookParser
from app.core.security import verify_meta_signature
from app.crud import instagram as crud_instagram
from app.db.models import User
from app.db.session import get_db
from app.schemas.instagram import InstagramAccountRead
from app.services import instagram_oauth
from app.tasks.instagram_comment_processing import process_inbound_comment

router = APIRouter()

logger = logging.getLogger(__name__)

# How long a merchant has to complete the Meta consent screen before the
# `state` token (which carries their identity through the redirect, since
# the callback is a plain browser navigation with no Authorization header)
# expires.
OAUTH_STATE_EXPIRE_MINUTES = 10


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
    logger.info(f"Instagram webhook payload: {data}")
    parsed_comments = InstagramWebhookParser.parse_inbound(data)
    logger.info(f"Parsed {len(parsed_comments)} comment(s) from payload")

    for item in parsed_comments:
        account = crud_instagram.get_account_by_ig_business_id(
            db, item["ig_business_id"]
        )
        if not account:
            logger.warning(
                f"No InstagramAccount found for ig_business_id={item['ig_business_id']!r} "
                f"(comment_id={item.get('comment_id')!r})"
            )
            continue

        if crud_instagram.comment_already_processed(db, item["comment_id"]):
            continue

        comment = crud_instagram.create_comment(db, account.id, item)

        if account.ai_enabled:
            process_inbound_comment.apply_async(args=[account.id, comment.id])

    return {"status": "ok"}


@router.get("/accounts", response_model=list[InstagramAccountRead])
async def list_accounts(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    return crud_instagram.get_accounts_by_user(db, current_user.id)


@router.get("/oauth/start")
async def start_oauth(current_user: User = Depends(get_current_user)):
    """Called by the dashboard's "Connect Instagram" button; the browser
    is then redirected to the returned URL for Meta's consent screen."""
    state = security.create_access_token(
        current_user.id, expires_delta=timedelta(minutes=OAUTH_STATE_EXPIRE_MINUTES)
    )
    return {"authorize_url": instagram_oauth.build_authorize_url(state)}


@router.get("/oauth/callback")
async def oauth_callback(
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """
    Meta redirects the merchant's browser here after the consent screen —
    a plain top-level navigation, so identity travels via the signed
    `state` token from /oauth/start rather than an Authorization header.
    Always redirects back to the dashboard; success/failure is conveyed
    via query params for the frontend to show a toast.
    """
    settings_url = f"{settings.DASHBOARD_URL}/dashboard/settings"

    if error or not code or not state:
        return RedirectResponse(f"{settings_url}?instagram=error")

    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return RedirectResponse(f"{settings_url}?instagram=error")

    try:
        short_lived_token = await instagram_oauth.exchange_code_for_short_lived_token(code)
        long_lived_token = await instagram_oauth.exchange_for_long_lived_token(short_lived_token)
        ig_user_id, username = await instagram_oauth.fetch_account_identity(long_lived_token)
    except instagram_oauth.InstagramOAuthError:
        return RedirectResponse(f"{settings_url}?instagram=error")

    crud_instagram.upsert_account(db, user_id, ig_user_id, long_lived_token, username)

    try:
        await instagram_oauth.subscribe_to_webhooks(long_lived_token)
    except instagram_oauth.InstagramOAuthError as e:
        # Don't fail the connection over this — the account is connected
        # either way, it just won't receive comment webhooks until this
        # succeeds (e.g. on a later reconnect).
        logger.error(f"Failed to subscribe {ig_user_id} to Instagram webhooks: {e}")

    return RedirectResponse(f"{settings_url}?instagram=connected")
