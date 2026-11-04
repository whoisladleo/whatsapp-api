"""Take-over replies: resuming a handoff makes Amara answer the customer's
last message immediately instead of waiting for their next one (#149).

All resume paths share this module — the WhatsApp/Instagram `/resume`
commands (including bare-pick and the alert's takeover button) and both
HTTP resume endpoints — so the verdict logic lives in exactly one place.
The take-over turn itself is just a re-dispatch of the normal inbound
pipeline on the last message row: WhatsApp's task takes the body
directly, Instagram's takes the row id with `resume=True` to skip the
already-won row claim. Everything else (lock, handoff check, quota,
rate limit, outage retry, verify hooks) runs unchanged, which is what
keeps a resume turn faithful to a live inbound one.

Two deliberate limits. Meta's 24-hour messaging window: a resume whose
last inbound message is older than that stays silent (the merchant is
told to reply manually) rather than firing a send Meta will reject.
And double-answer races: a customer message landing mid-resume-reply
gets its own turn — the locks serialize, but order isn't guaranteed.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RESUME_REPLY_WINDOW_SECONDS = 24 * 60 * 60


@dataclass
class ResumeVerdict:
    channel: str  # "whatsapp" | "instagram"
    answerable: bool
    reason: str  # "answerable" | "stale" | "no_inbound"
    account_id: uuid.UUID | None = None
    customer_ref: str | None = None  # phone (WA) or customer uuid hex (IG)
    row_id: uuid.UUID | None = None  # IG dm row id for the re-dispatch
    body: str | None = None  # WA last message body for the re-dispatch


def _window_start() -> datetime:
    return datetime.utcnow() - timedelta(seconds=RESUME_REPLY_WINDOW_SECONDS)


def verdict_for_whatsapp(db: Session, account_id: uuid.UUID, phone: str) -> ResumeVerdict:
    """Answerability for a WhatsApp resume: the latest inbound row for this
    (account, phone) pair — the same conversation key the history query in
    generate_response uses — and whether it's inside the reply window."""
    from app.db.models import Message

    row = (
        db.query(Message)
        .filter(
            Message.account_id == account_id,
            Message.direction == "inbound",
            Message.from_phone == phone,
        )
        .order_by(Message.timestamp.desc())
        .first()
    )
    if row is None:
        return ResumeVerdict("whatsapp", False, "no_inbound", account_id, phone)
    if (row.timestamp or datetime.min) < _window_start():
        return ResumeVerdict("whatsapp", False, "stale", account_id, phone)
    return ResumeVerdict("whatsapp", True, "answerable", account_id, phone, body=row.body or "")


def verdict_for_instagram(db: Session, customer_id: uuid.UUID) -> ResumeVerdict:
    """Answerability for an Instagram resume, keyed by customer like the DM
    pipeline itself. The account id comes off the customer row so take-over
    taps (which carry only the customer id) need no extra lookup."""
    from app.db.models import InstagramCustomer, InstagramDirectMessage

    row = (
        db.query(InstagramDirectMessage)
        .filter(
            InstagramDirectMessage.customer_id == customer_id,
            InstagramDirectMessage.direction == "inbound",
        )
        .order_by(InstagramDirectMessage.created_at.desc())
        .first()
    )
    customer = db.query(InstagramCustomer).filter(InstagramCustomer.id == customer_id).first()
    account_id = customer.account_id if customer else None
    if row is None:
        return ResumeVerdict("instagram", False, "no_inbound", account_id, str(customer_id))
    if (row.created_at or datetime.min) < _window_start():
        return ResumeVerdict("instagram", False, "stale", account_id, str(customer_id))
    return ResumeVerdict(
        "instagram", True, "answerable", account_id, str(customer_id), row_id=row.id
    )


def merchant_resume_line(display: str, reply_hint: str, verdict: ResumeVerdict) -> str:
    """`/resume` reply text after the state flip. display is the phone or
    @username the merchant typed; reply_hint is the manual /reply command
    for the stale case (e.g. "/reply 2557... <message>")."""
    if verdict.answerable:
        return f"Conversation with {display} resumed. Amara is replying to their last message now."
    if verdict.reason == "stale":
        return (
            f"Conversation with {display} resumed. Their last message is over 24 hours old, "
            f"so Amara stays silent — reply manually with {reply_hint}."
        )
    return (
        f"Conversation with {display} resumed. There's no customer message to answer, "
        "so Amara will respond to their next message."
    )


def takeover_resume_line(name: str, note: str, reply_hint: str, verdict: ResumeVerdict) -> str:
    """Takeover-button reply text — same verdicts, the alert tap's voice."""
    if verdict.answerable:
        return f"Amara is back on {name}{note} — she's replying to their last message now."
    if verdict.reason == "stale":
        return (
            f"Amara is back on {name}{note} — but their last message is over 24 hours old, "
            f"so reply manually with {reply_hint}."
        )
    return f"Amara is back on {name}{note} — she'll reply to their next message."


def dispatch_resume_reply(verdict: ResumeVerdict) -> bool:
    """Queues the take-over turn for an answerable verdict. Returns whether
    anything was queued. Task imports stay local: this service is imported
    by the merchant handler, which the inbound tasks already import."""
    if not verdict.answerable:
        return False
    if verdict.channel == "whatsapp":
        from app.tasks.webhook_processing import process_inbound_message

        process_inbound_message.apply_async(
            args=[str(verdict.account_id), verdict.customer_ref, verdict.body or ""]
        )
    else:
        from app.tasks.instagram_dm_processing import process_inbound_dm

        process_inbound_dm.apply_async(
            args=[str(verdict.account_id), verdict.customer_ref, str(verdict.row_id), 0, None, 0, True]
        )
    logger.info(
        "Queued resume reply for %s conversation %s", verdict.channel, verdict.customer_ref
    )
    return True
