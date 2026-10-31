import json
import logging
import os
import uuid

import redis
from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.core.security import normalize_phone
from app.db.models import (
    ConversationState,
    ConversationContext,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Message,
    User,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)

redis_client = redis.from_url(
    os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
)

# Quick-reply buttons on the handoff_notification template (#88). Template
# button taps don't reliably echo a custom payload — Meta may return the
# button title as the id instead — so routing accepts both.
BUTTON_REPLY = "handoff_reply"
BUTTON_DONE = "handoff_done"
BUTTON_TAKEOVER = "handoff_takeover"

BUTTON_TITLES = {
    # Plain texts as created in WhatsApp Manager (no custom payloads there).
    "Reply": BUTTON_REPLY,
    "Done": BUTTON_DONE,
    "Amara, take over.": BUTTON_TAKEOVER,
    "Amara, take over": BUTTON_TAKEOVER,
    # Emoji variants, accepted as aliases.
    "✏️ Reply": BUTTON_REPLY,
    "✅ Done": BUTTON_DONE,
    "🤖 Amara, take over": BUTTON_TAKEOVER,
}


def normalize_button_id(button_id: str | None, title: str | None) -> str | None:
    if button_id in (BUTTON_REPLY, BUTTON_DONE, BUTTON_TAKEOVER):
        return button_id
    if button_id in BUTTON_TITLES:
        return BUTTON_TITLES[button_id]
    if title and title.strip() in BUTTON_TITLES:
        return BUTTON_TITLES[title.strip()]
    # Title drift (case/extra spaces from a re-created template) must not
    # silently drop a merchant's tap — match case-insensitively (#96).
    folded = {key.casefold(): value for key, value in BUTTON_TITLES.items()}
    if button_id and button_id.strip().casefold() in folded:
        return folded[button_id.strip().casefold()]
    if title and title.strip().casefold() in folded:
        return folded[title.strip().casefold()]
    return button_id

REPLY_MODE_TTL_SECONDS = 900  # 15 minutes to send the message
ALERT_TARGET_TTL_SECONDS = 7 * 24 * 3600  # taps land within days, not weeks


def _alert_key(alert_wamid: str) -> str:
    return f"handoff_alert:{alert_wamid}"


def _mode_key(merchant_id: uuid.UUID) -> str:
    return f"handoff_reply_mode:{merchant_id}"


def resolve_merchant(db: Session, from_phone: str) -> User | None:
    """The tap/reply sender — strangers resolve to None and stay silent.
    Compares normalized-to-normalized, the same as the is_merchant check
    in webhook_processing: a username stored pre-normalization ("0712…",
    "+255…") must still match, or taps die silent while texts work (#96).
    The scan only runs when the indexed lookup misses, and taps are
    merchant-rare, so the fallback costs nothing in practice."""
    needle = normalize_phone(from_phone)
    user = db.query(User).filter(User.username == needle).first()
    if user is not None:
        return user
    for candidate_id, username in db.query(User.id, User.username).all():
        if username and normalize_phone(username) == needle:
            return db.get(User, candidate_id)
    return None


def remember_alert(alert_wamid: str, target: dict) -> None:
    """
    Maps a sent handoff alert to its customer, so a button tap's context
    message id resolves to the exact conversation (template button payloads
    are static — they can't carry the customer themselves). Best-effort:
    a Redis outage just falls back to most-recent-pending at tap time.
    """
    try:
        redis_client.setex(_alert_key(alert_wamid), ALERT_TARGET_TTL_SECONDS, json.dumps(target))
    except redis.RedisError as e:
        logger.error(f"Failed to store handoff alert target: {e}")


def lookup_alert(alert_wamid: str) -> dict | None:
    try:
        raw = redis_client.get(_alert_key(alert_wamid))
    except redis.RedisError:
        return None
    if not raw:
        return None
    try:
        target = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return target if isinstance(target, dict) else None


def list_pending_handoffs(db: Session, merchant: User) -> list:
    """
    Merchant's HUMAN_HANDOFF conversations across both channels, most recent
    first. Each entry: {channel, ref, name, handoff_at, account_id?,
    customer_id?} — everything arming, relaying and takeover need.
    """
    pending = []

    business_accounts = (
        db.query(WhatsAppAccount)
        .filter(
            WhatsAppAccount.user_id == merchant.id,
            WhatsAppAccount.is_platform_account == False,  # noqa: E712
        )
        .all()
    )
    for account in business_accounts:
        has_history = exists().where(
            Message.from_phone == Customer.phone_number,
            Message.account_id == account.id,
        )
        rows = (
            db.query(Customer, ConversationContext)
            .join(ConversationContext, ConversationContext.customer_id == Customer.id)
            .filter(ConversationContext.state == ConversationState.HUMAN_HANDOFF, has_history)
            .all()
        )
        for customer, context in rows:
            pending.append(
                {
                    "channel": "whatsapp",
                    "ref": customer.phone_number,
                    "name": customer.phone_number,
                    "handoff_at": context.handoff_at.isoformat() if context.handoff_at else None,
                    "account_id": str(account.id),
                    "customer_id": None,
                }
            )

    ig_accounts = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == merchant.id, InstagramAccount.access_token != "")
        .all()
    )
    for ig_account in ig_accounts:
        rows = (
            db.query(InstagramCustomer, InstagramConversationContext)
            .join(
                InstagramConversationContext,
                InstagramConversationContext.customer_id == InstagramCustomer.id,
            )
            .filter(
                InstagramCustomer.account_id == ig_account.id,
                InstagramConversationContext.state == ConversationState.HUMAN_HANDOFF,
            )
            .all()
        )
        for customer, context in rows:
            ref = customer.username or customer.ig_scoped_id
            pending.append(
                {
                    "channel": "instagram",
                    "ref": ref,
                    "name": f"@{ref}",
                    "handoff_at": context.handoff_at.isoformat() if context.handoff_at else None,
                    "account_id": None,
                    "customer_id": str(customer.id),
                }
            )

    pending.sort(key=lambda entry: entry["handoff_at"] or "", reverse=True)
    return pending


def target_still_pending(db: Session, target: dict) -> bool:
    """An alert tap names a conversation that may since have been resumed."""
    if target.get("channel") == "instagram" and target.get("customer_id"):
        try:
            customer_uuid = uuid.UUID(target["customer_id"])
        except ValueError:
            return False
        context = (
            db.query(InstagramConversationContext)
            .filter(InstagramConversationContext.customer_id == customer_uuid)
            .first()
        )
        return context is not None and context.state == ConversationState.HUMAN_HANDOFF
    if target.get("channel") == "whatsapp" and target.get("ref"):
        customer = db.query(Customer).filter(Customer.phone_number == target["ref"]).first()
        if not customer:
            return False
        context = (
            db.query(ConversationContext)
            .filter(ConversationContext.customer_id == customer.id)
            .first()
        )
        return context is not None and context.state == ConversationState.HUMAN_HANDOFF
    return False


def arm_reply_mode(merchant_id: uuid.UUID, target: dict) -> bool:
    try:
        redis_client.setex(_mode_key(merchant_id), REPLY_MODE_TTL_SECONDS, json.dumps(target))
    except redis.RedisError as e:
        logger.error(f"Failed to arm handoff reply mode: {e}")
        return False
    return True


def get_reply_mode(merchant_id: uuid.UUID) -> dict | None:
    try:
        raw = redis_client.get(_mode_key(merchant_id))
    except redis.RedisError:
        return None
    if not raw:
        return None
    try:
        target = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return target if isinstance(target, dict) else None


def clear_reply_mode(merchant_id: uuid.UUID) -> None:
    try:
        redis_client.delete(_mode_key(merchant_id))
    except redis.RedisError as e:
        logger.error(f"Failed to clear handoff reply mode: {e}")


def _resolve_tap_target(db: Session, merchant: User, context_wamid: str | None):
    """
    Returns (target, note): the exact alert conversation when the tap
    carries a known, still-pending context id, else the most recent pending
    handoff. target is None when nothing is waiting at all.
    """
    if context_wamid:
        target = lookup_alert(context_wamid)
        if target is not None:
            if target_still_pending(db, target):
                return target, ""
            return None, f"{target.get('name', 'That conversation')} is no longer waiting — nothing to do."
    pending = list_pending_handoffs(db, merchant)
    if not pending:
        return None, "No customers are waiting for you right now."
    note = f" (most recent of {len(pending)} waiting)" if len(pending) > 1 else ""
    return pending[0], note


async def handle_button(
    db: Session, merchant: User | None, button_id: str, context_wamid: str | None
) -> str | None:
    """Routes a handoff template button tap. None means stay silent
    (unknown sender or unknown button — not our alert)."""
    if merchant is None:
        return None
    if button_id == BUTTON_REPLY:
        target, note = _resolve_tap_target(db, merchant, context_wamid)
        if target is None:
            return note
        if not arm_reply_mode(merchant.id, target):
            return "Couldn't start reply mode — reply with /reply instead."
        return (
            f"You're replying to {target['name']}{note} — "
            "send your message here, or /cancel."
        )
    if button_id == BUTTON_DONE:
        clear_reply_mode(merchant.id)
        target, _ = _resolve_tap_target(db, merchant, context_wamid)
        if target is None:
            return "Nothing in progress right now."
        return (
            f"Okay — {target['name']} stays paused with you. "
            "Send /resume to hand them back to Amara."
        )
    if button_id == BUTTON_TAKEOVER:
        clear_reply_mode(merchant.id)
        target, note = _resolve_tap_target(db, merchant, context_wamid)
        if target is None:
            return note
        return _takeover(db, merchant, target, note)
    # A resolved merchant tapping *something* on the platform chat is never
    # a stranger — staying silent strands them with no relay and no hint
    # (#96). Strangers (merchant None) still get silence above.
    logger.warning("Unrecognized handoff tap id=%r from merchant %s", button_id, merchant.id)
    return "That tap didn't come through — reply with /reply instead."


def _takeover(db: Session, merchant: User, target: dict, note: str) -> str:
    from app.crud import message as crud_message
    from app.crud import instagram as crud_instagram

    if target["channel"] == "instagram":
        try:
            crud_instagram.resume_conversation(db, uuid.UUID(target["customer_id"]))
        except crud_instagram.ConversationNotInHandoffError:
            return f"{target['name']} is not currently waiting."
        return f"Amara is back on {target['name']}{note} — she'll reply to their next message."
    try:
        crud_message.resume_conversation_for_account(
            db, uuid.UUID(target["account_id"]), target["ref"]
        )
    except (crud_message.ConversationNotFoundError, crud_message.ConversationNotInHandoffError) as e:
        return str(e)
    return f"Amara is back on {target['name']}{note} — she'll reply to their next message."


async def consume_reply_mode(db: Session, merchant: User, message_body: str) -> str | None:
    """
    Relays one armed reply-mode message to its customer. None when no mode
    is armed (caller falls through to the normal flow). Single-shot: a
    delivered reply — or a target that no longer exists — disarms; a failed
    send stays armed so the merchant can retry or /cancel.
    """
    target = get_reply_mode(merchant.id)
    if target is None:
        return None
    if message_body.strip().lower() == "/cancel":
        clear_reply_mode(merchant.id)
        return "Cancelled — nothing was sent."
    if target.get("channel") == "instagram":
        result = await relay_to_instagram_customer(db, merchant, target["ref"], message_body)
    else:
        account = (
            db.query(WhatsAppAccount)
            .filter(
                WhatsAppAccount.id == uuid.UUID(target["account_id"]),
                WhatsAppAccount.user_id == merchant.id,
            )
            .first()
        )
        if not account:
            clear_reply_mode(merchant.id)
            return "That business number is no longer connected — reply mode off."
        result = await relay_to_whatsapp_customer(db, account, target["ref"], message_body)
    if result.startswith("Sent to") or result.startswith("No "):
        clear_reply_mode(merchant.id)
    else:
        result += " (still armed — fix and retry, or /cancel)"
    return result


async def relay_to_whatsapp_customer(db: Session, account: WhatsAppAccount, phone: str, reply_text: str) -> str:
    """
    Sends `reply_text` as the merchant's own manual reply to a WhatsApp
    customer, via the business account's credentials. Requires prior message
    history with *this* account (Customer is global, not merchant-scoped)
    rather than trusting a typed phone number outright, since a raw text
    command is far more typo-prone than a dashboard form.
    """
    from app.core.provider import WhatsAppProvider
    from app.crud import message as crud_message

    has_history = (
        db.query(Message)
        .filter(Message.from_phone == phone, Message.account_id == account.id)
        .first()
    )
    if not has_history:
        return f"No conversation with {phone} found for this account."

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(phone, reply_text)
    except Exception as e:
        return f"Failed to send reply to {phone}: {e}"

    if not resp or "messages" not in resp:
        error_detail = resp.get("error", {}).get("message", "unknown error") if resp else "unknown error"
        return f"Failed to send reply to {phone}: {error_detail}"

    crud_message.create_message(
        db,
        {
            "whatsapp_message_id": resp["messages"][0].get("id"),
            "to_phone": phone,
            "type": "text",
            "body": reply_text,
            "direction": "outbound",
            "status": "sent",
            "raw": resp,
        },
        account.id,
    )
    return f"Sent to {phone}."


async def relay_to_instagram_customer(db: Session, merchant: User, ig_username: str, reply_text: str) -> str:
    """
    Lets a merchant reply to an Instagram DM customer from their WhatsApp
    number — this WhatsApp account is how the merchant talks *to*
    Amara/Taskflow, even for a conversation actually happening on a
    different channel. Scoped to this merchant's own connected
    InstagramAccount and an InstagramCustomer who has actually messaged it.

    `ig_username` also matches against ig_scoped_id as a fallback — the
    handoff alert uses whichever one it actually has for that customer,
    which is the scoped id for a brand-new customer whose display-name
    backfill hasn't run yet.
    """
    from app.core.instagram_provider import InstagramProvider
    from app.crud import instagram as crud_instagram

    merchant_id = merchant.id if merchant is not None else None
    ig_account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == merchant_id, InstagramAccount.access_token != "")
        .first()
    )
    if not ig_account:
        return "No Instagram account connected."

    ig_customer = (
        db.query(InstagramCustomer)
        .filter(
            InstagramCustomer.account_id == ig_account.id,
            (InstagramCustomer.username == ig_username) | (InstagramCustomer.ig_scoped_id == ig_username),
        )
        .first()
    )
    if not ig_customer:
        return f"No Instagram conversation with @{ig_username} found."

    provider = InstagramProvider(ig_account.access_token)
    try:
        resp = await provider.send_direct_message(
            ig_account.ig_business_id, ig_customer.ig_scoped_id, reply_text, tag="HUMAN_AGENT"
        )
    except Exception as e:
        return f"Failed to send reply to @{ig_username}: {e}"

    if "error" in resp:
        return f"Failed to send reply to @{ig_username}: {resp['error'].get('message', 'unknown error')}"

    crud_instagram.create_direct_message(
        db,
        account_id=ig_account.id,
        customer_id=ig_customer.id,
        direction="outbound",
        body=reply_text,
        ig_message_id=resp.get("message_id"),
    )
    return f"Sent to @{ig_username}."
