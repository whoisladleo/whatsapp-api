from datetime import timedelta
from typing import Optional

import uuid
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core import security
from app.core.config import settings
from app.core.instagram_provider import InstagramProvider
from app.core.instagram_webhook_parser import InstagramWebhookParser
from app.core.security import verify_meta_signature
from app.crud import instagram as crud_instagram
from app.db.models import InstagramAccount, InstagramComment, InstagramDirectMessage, User
from app.db.session import get_db
from app.schemas.instagram import (
    InstagramAccountRead,
    InstagramConversationSummary,
    InstagramMessageRead,
    InstagramMessageSend,
    InstagramPendingHandoffRead,
)
from app.services import instagram_oauth
from app.tasks.instagram_comment_processing import process_inbound_comment
from app.tasks.instagram_dm_processing import process_inbound_dm
from app.tasks.instagram_token_refresh import refresh_account_token
from app.tasks.webhook_processing import apply_async_or_rollback

router = APIRouter()


async def get_instagram_account(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> InstagramAccount:
    """
    Unlike WhatsApp's get_whatsapp_account, there's no header/account-id to
    resolve — a Taskflow user has at most one connected InstagramAccount
    (see upsert_account), so it's looked up directly off the JWT.
    """
    account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == current_user.id, InstagramAccount.access_token != "")
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="No Instagram account connected")
    return account

# How long a merchant has to complete the Meta consent screen before the
# `state` token (which carries their identity through the redirect, since
# the callback is a plain browser navigation with no Authorization header)
# expires.
OAUTH_STATE_EXPIRE_MINUTES = 10


async def _fetch_instagram_display_name(account: InstagramAccount, igsid: str) -> Optional[str]:
    """
    Best-effort lookup for a DM sender's real name, since the messaging
    webhook only ever gives us their IGSID (see
    InstagramWebhookParser.parse_inbound_messages) — unlike the comments
    webhook, which includes from.username directly. Failures (network,
    permission, deactivated account, etc.) are swallowed: worst case the
    customer keeps showing as "Instagram user" in the dashboard, same as
    today, rather than breaking DM processing over a cosmetic lookup.
    """
    provider = InstagramProvider(account.access_token)
    try:
        profile = await provider.get_user_profile(igsid)
    except Exception as e:
        print(f"Failed to fetch Instagram profile for {igsid}: {e}")
        return None
    if "error" in profile:
        print(f"Instagram profile lookup for {igsid} returned an error: {profile}")
        return None
    return profile.get("username") or profile.get("name")


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


def _dispatch_token_refresh_if_stale(account: InstagramAccount) -> None:
    """
    Best-effort lazy token refresh (#80). Refresh is auxiliary — its
    failure (or the broker's) must never break the message path, so this
    never raises.
    """
    if not crud_instagram.token_needs_refresh(account):
        return
    try:
        refresh_account_token.apply_async(args=[str(account.id)])
    except Exception as e:
        print(f"Failed to dispatch token refresh for account {account.id}: {e}")


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
    elif not settings.ALLOW_UNSIGNED_WEBHOOKS:
        raise HTTPException(status_code=403, detail="Webhook signature verification is not configured")
    else:
        print(
            "WARNING: INSTAGRAM_APP_SECRET is not configured and "
            "ALLOW_UNSIGNED_WEBHOOKS=true; accepting unsigned webhook payloads."
        )

    data = await request.json()
    print(f"Instagram webhook payload: {data}")
    parsed_comments = InstagramWebhookParser.parse_inbound(data)
    print(f"Parsed {len(parsed_comments)} comment(s) from payload")

    for item in parsed_comments:
        account = crud_instagram.get_account_by_ig_business_id(
            db, item["ig_business_id"]
        )
        if not account:
            print(
                f"No InstagramAccount found for ig_business_id={item['ig_business_id']!r} "
                f"(comment_id={item.get('comment_id')!r})"
            )
            continue

        _dispatch_token_refresh_if_stale(account)

        if crud_instagram.comment_already_processed(db, item["comment_id"]):
            continue

        comment = crud_instagram.create_comment(db, account.id, item)

        if account.ai_enabled:
            apply_async_or_rollback(
                process_inbound_comment,
                [str(account.id), str(comment.id)],
                db, InstagramComment, comment.id,
            )

    parsed_messages = InstagramWebhookParser.parse_inbound_messages(data)
    print(f"Parsed {len(parsed_messages)} DM(s) from payload")

    for item in parsed_messages:
        account = crud_instagram.get_account_by_ig_business_id(
            db, item["ig_business_id"]
        )
        if not account:
            print(
                f"No InstagramAccount found for ig_business_id={item['ig_business_id']!r} "
                f"(ig_message_id={item.get('ig_message_id')!r})"
            )
            continue

        _dispatch_token_refresh_if_stale(account)

        if crud_instagram.dm_already_processed(db, item["ig_message_id"]):
            continue

        customer = crud_instagram.get_or_create_customer(
            db, account.id, item["sender_id"]
        )
        if not customer.username:
            fetched_username = await _fetch_instagram_display_name(account, item["sender_id"])
            if fetched_username:
                customer = crud_instagram.get_or_create_customer(
                    db, account.id, item["sender_id"], username=fetched_username
                )
        dm = crud_instagram.create_direct_message(
            db,
            account_id=account.id,
            customer_id=customer.id,
            direction="inbound",
            body=item["body"],
            ig_message_id=item["ig_message_id"],
            attachments=item.get("attachments"),
        )

        if account.ai_enabled:
            apply_async_or_rollback(
                process_inbound_dm,
                [str(account.id), str(customer.id), str(dm.id)],
                db, InstagramDirectMessage, dm.id,
            )

    parsed_echoes = InstagramWebhookParser.parse_outbound_echoes(data)
    print(f"Parsed {len(parsed_echoes)} echo(s) from payload")

    for item in parsed_echoes:
        account = crud_instagram.get_account_by_ig_business_id(
            db, item["ig_business_id"]
        )
        if not account:
            print(
                f"No InstagramAccount found for ig_business_id={item['ig_business_id']!r} "
                f"(ig_message_id={item.get('ig_message_id')!r})"
            )
            continue

        _dispatch_token_refresh_if_stale(account)

        # Echo of a message Amara (or the dashboard) sent via the API: its
        # mid was already saved at send time, so there is nothing to do —
        # same as when the parser dropped every echo. This check also
        # dedupes redeliveries of the hand-typed echoes saved below.
        if crud_instagram.dm_already_processed(db, item["ig_message_id"]):
            continue

        customer = crud_instagram.get_or_create_customer(
            db, account.id, item["sender_id"]
        )
        if not customer.username:
            fetched_username = await _fetch_instagram_display_name(account, item["sender_id"])
            if fetched_username:
                customer = crud_instagram.get_or_create_customer(
                    db, account.id, item["sender_id"], username=fetched_username
                )
        # Read BEFORE the save below: the just-saved echo is itself
        # outbound, so checking after would mistake every opener for a
        # mid-chat takeover.
        amara_replied = crud_instagram.conversation_has_outbound_message(db, customer.id)
        crud_instagram.create_direct_message(
            db,
            account_id=account.id,
            customer_id=customer.id,
            direction="outbound",
            body=item["body"],
            ig_message_id=item["ig_message_id"],
            attachments=item.get("attachments"),
        )
        if amara_replied:
            # A person typed in the Instagram inbox after Amara already
            # replied — the merchant is taking over this thread, so Amara
            # goes silent until /resume.
            crud_instagram.set_human_handoff(db, customer.id)
        # Either way, no reply task is dispatched: a shop-side message is
        # never answered — the opener is answered only when the customer
        # replies to it, with the opener already in history.

    return {"status": "ok"}


@router.get("/accounts", response_model=list[InstagramAccountRead])
async def list_accounts(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    return crud_instagram.get_accounts_by_user(db, current_user.id)


@router.delete("/accounts/{account_id}")
async def disconnect_account(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Revokes Meta's webhook subscription and clears the local access token —
    doesn't delete the InstagramAccount row itself (see
    crud.instagram.disconnect_account for why: comments/DMs/orders already
    reference it). Reconnecting later revives the same row.
    """
    account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.id == account_id, InstagramAccount.user_id == current_user.id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="Instagram account not found")

    try:
        await instagram_oauth.unsubscribe_from_webhooks(account.access_token)
    except instagram_oauth.InstagramOAuthError as e:
        print(f"Failed to unsubscribe {account.id} from Instagram webhooks: {e}")

    crud_instagram.disconnect_account(db, account)
    return {"status": "disconnected"}


@router.get("/oauth/start")
async def start_oauth(current_user: User = Depends(get_current_user)):
    """Called by the dashboard's "Connect Instagram" button; the browser
    is then redirected to the returned URL for Meta's consent screen."""
    state = security.create_access_token(
        current_user.id, expires_delta=timedelta(minutes=OAUTH_STATE_EXPIRE_MINUTES)
    )
    try:
        authorize_url = instagram_oauth.build_authorize_url(state)
    except instagram_oauth.InstagramOAuthError as e:
        # Missing app id / redirect env — say so plainly instead of sending
        # Meta a broken URL (#84).
        raise HTTPException(status_code=500, detail=str(e))
    return {"authorize_url": authorize_url}


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
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return RedirectResponse(f"{settings_url}?instagram=error")

    try:
        short_lived_token = await instagram_oauth.exchange_code_for_short_lived_token(code)
        print(f"Short-lived token obtained, tail={short_lived_token[-12:]!r}")
        long_lived_token = await instagram_oauth.exchange_for_long_lived_token(short_lived_token)
        print(f"Long-lived token obtained, tail={long_lived_token[-12:]!r}")
        ig_user_id, username = await instagram_oauth.fetch_account_identity(long_lived_token)
        print(f"fetch_account_identity succeeded: ig_user_id={ig_user_id} username={username!r}")
    except instagram_oauth.InstagramOAuthError as e:
        print(f"Instagram OAuth exchange failed: {e}")
        return RedirectResponse(f"{settings_url}?instagram=error")

    crud_instagram.upsert_account(db, user_id, ig_user_id, long_lived_token, username)
    print(f"Stored InstagramAccount for ig_user_id={ig_user_id}, tail={long_lived_token[-12:]!r}")

    # Immediate re-check, milliseconds later, using the exact same token —
    # to see whether it's already dead by the time this request finishes,
    # versus dying sometime after.
    try:
        recheck_id, recheck_username = await instagram_oauth.fetch_account_identity(long_lived_token)
        print(f"Immediate re-check OK: {recheck_id} {recheck_username!r}")
    except instagram_oauth.InstagramOAuthError as e:
        print(f"Immediate re-check FAILED (token already dead within the same request): {e}")

    try:
        await instagram_oauth.subscribe_to_webhooks(long_lived_token)
    except instagram_oauth.InstagramOAuthError as e:
        # Don't fail the connection over this — the account is connected
        # either way, it just won't receive comment webhooks until this
        # succeeds (e.g. on a later reconnect).
        print(f"Failed to subscribe {ig_user_id} to Instagram webhooks: {e}")

    return RedirectResponse(f"{settings_url}?instagram=connected")


@router.get("/conversations", response_model=list[InstagramConversationSummary])
def get_conversations(
    account: InstagramAccount = Depends(get_instagram_account),
    db: Session = Depends(get_db),
):
    return crud_instagram.get_conversations_for_account(db, account.id)


@router.get("/handoffs", response_model=list[InstagramPendingHandoffRead])
def get_pending_handoffs(
    account: InstagramAccount = Depends(get_instagram_account),
    db: Session = Depends(get_db),
):
    return crud_instagram.get_pending_handoffs_for_account(db, account.id)


@router.post("/handoffs/{customer_id}/resume")
def resume_handoff(
    customer_id: uuid.UUID,
    account: InstagramAccount = Depends(get_instagram_account),
    db: Session = Depends(get_db),
):
    try:
        crud_instagram.get_customer_for_account(db, account.id, customer_id)
        crud_instagram.resume_conversation(db, customer_id)
    except crud_instagram.CustomerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except crud_instagram.ConversationNotInHandoffError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "resumed", "customer_id": customer_id}


@router.get("/messages", response_model=list[InstagramMessageRead])
def get_message_history(
    customer_id: uuid.UUID,
    limit: int = 100,
    account: InstagramAccount = Depends(get_instagram_account),
    db: Session = Depends(get_db),
):
    try:
        crud_instagram.get_customer_for_account(db, account.id, customer_id)
    except crud_instagram.CustomerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return crud_instagram.get_dm_history(db, customer_id, limit=limit)


@router.post("/messages/send")
async def send_message(
    payload: InstagramMessageSend,
    account: InstagramAccount = Depends(get_instagram_account),
    db: Session = Depends(get_db),
):
    """
    A merchant manually replying from the dashboard — most useful once a
    conversation is in HUMAN_HANDOFF, but not restricted to that state
    (a merchant may want to jump in at any point). Amara stays silent on
    this thread going forward only if the conversation is actually in
    handoff; sending a message here doesn't itself change conversation state.
    """
    try:
        customer = crud_instagram.get_customer_for_account(db, account.id, payload.customer_id)
    except crud_instagram.CustomerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    provider = InstagramProvider(account.access_token)
    response = await provider.send_direct_message(
        account.ig_business_id, customer.ig_scoped_id, payload.message, tag="HUMAN_AGENT"
    )
    if "error" in response:
        # Surface Meta's actual error (e.g. outside the messaging window,
        # invalid/expired token) instead of a generic message — the
        # dashboard's sendInstagramReply action already renders
        # errorData?.detail directly to the merchant, so this flows
        # straight through. Discarding it here was a real gap: neither the
        # dashboard nor logs showed why a send failed, and a failed attempt
        # is never persisted (InstagramDirectMessage has no status column).
        #
        # 422, not 502: this is Meta's API rejecting a well-formed request,
        # not a gateway/infra failure — confirmed live that 502 gets its
        # body swallowed and replaced by whatever reverse proxy sits in
        # front of this API (Coolify/nginx/Cloudflare all commonly rewrite
        # 502/503/504 with their own generic error page), so the detail
        # above never actually reached the dashboard despite being set.
        meta_detail = response.get("error", {}).get("message") or "Failed to send Instagram message"
        raise HTTPException(status_code=422, detail=meta_detail)

    message = crud_instagram.create_direct_message(
        db,
        account_id=account.id,
        customer_id=customer.id,
        direction="outbound",
        body=payload.message,
        ig_message_id=response.get("message_id"),
    )
    return {"status": "sent", "message_id": message.id}
