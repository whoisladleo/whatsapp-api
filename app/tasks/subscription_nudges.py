import asyncio
import logging
from datetime import datetime, timedelta

import app.core.rate_limit as rate_limit_module
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.provider import WhatsAppProvider
from app.db.models import (
    AmaraEvent,
    AmaraEventKind,
    InstagramAccount,
    User,
    WhatsAppAccount,
)
from app.db.session import SessionLocal
from app.services import usage
from app.services.subscription_access import (
    get_platform_plans,
    is_merchant_allowed_ai,
    sync_ai_access_for_merchant,
)

logger = logging.getLogger(__name__)

# At most one sweep per rolling 24h — the guard is claimed with SET NX (see
# maybe_dispatch_daily_sweep), so overlapping webhook traffic can't queue
# two sweeps for the same day.
SWEEP_GUARD_KEY = "subscription_nudge_sweep:claimed"
SWEEP_GUARD_TTL_SECONDS = 24 * 60 * 60

# Per-merchant schedule: nudge 1 goes immediately, nudge 2 no sooner than 2
# days later, nudge 3 no sooner than 5 days after that (day 7), then weekly.
# 30 sends over ~7 months, then silence — nagging forever burns goodwill
# and WhatsApp money on a merchant who will never convert.
MAX_NUDGES = 30
_FIRST_GAP_DAYS = 2
_SECOND_GAP_DAYS = 5
_WEEKLY_GAP_DAYS = 7


def maybe_dispatch_daily_sweep() -> None:
    """
    Called at the end of every verified inbound webhook (WhatsApp and
    Instagram). There is no Celery Beat in this stack, so the sweep is
    traffic-triggered instead of scheduled: the first webhook in each
    rolling 24h window claims a Redis guard and queues the sweep; every
    other call no-ops. Never raises — a sweep must not break webhooks.
    """
    try:
        claimed = rate_limit_module._redis_client.set(
            SWEEP_GUARD_KEY, "1", nx=True, ex=SWEEP_GUARD_TTL_SECONDS
        )
    except Exception:
        return
    if not claimed:
        return
    try:
        send_subscription_nudges.apply_async()
    except Exception:
        logger.exception("Failed to dispatch subscription nudge sweep")


def _nudge_history(db, user_id) -> list:
    """Past SUBSCRIPTION_NUDGE events for this merchant, newest first."""
    return (
        db.query(AmaraEvent)
        .filter(
            AmaraEvent.merchant_id == user_id,
            AmaraEvent.kind == AmaraEventKind.SUBSCRIPTION_NUDGE,
        )
        .order_by(AmaraEvent.created_at.desc())
        .all()
    )


def _nudge_due(nudges: list, now: datetime) -> bool:
    """True when this merchant's next nudge in the schedule is due."""
    if len(nudges) >= MAX_NUDGES:
        return False
    if not nudges:
        return True
    last_sent = nudges[0].created_at
    if last_sent is None:
        return True
    if len(nudges) == 1:
        gap_days = _FIRST_GAP_DAYS
    elif len(nudges) == 2:
        gap_days = _SECOND_GAP_DAYS
    else:
        gap_days = _WEEKLY_GAP_DAYS
    return (now - last_sent) >= timedelta(days=gap_days)


def _has_connected_channel(db, user_id) -> bool:
    """Has the merchant connected anything Amara could sell on — any
    WhatsApp account, or an Instagram account that isn't disconnected."""
    if (
        db.query(WhatsAppAccount).filter(WhatsAppAccount.user_id == user_id).first()
        is not None
    ):
        return True
    return (
        db.query(InstagramAccount)
        .filter(
            InstagramAccount.user_id == user_id,
            InstagramAccount.access_token.isnot(None),
        )
        .first()
        is not None
    )


def _send_nudge(to_phone: str) -> bool:
    """One nudge via platform credentials; True iff Meta accepted it. A
    rejection (e.g. template not approved yet) is a skip, not a failure —
    nothing is recorded, so the sweep simply tries again another day."""
    provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
    # No components: the approved template is static body text plus a static
    # URL button ("Taskflow OS" → dashboard settings). Sending body
    # parameters for a template with zero variables gets rejected by Meta,
    # and static buttons take no parameters — only dynamic-URL ones do.
    try:
        resp = asyncio.run(
            provider.send_template(
                to_phone,
                settings.WHATSAPP_NUDGE_TEMPLATE_NAME,
                language_code=settings.WHATSAPP_NUDGE_TEMPLATE_LANGUAGE,
            )
        )
    except Exception as e:
        logger.error(f"Subscription nudge send failed for {to_phone}: {e}")
        return False
    if not isinstance(resp, dict) or "messages" not in resp:
        logger.error(f"Subscription nudge rejected for {to_phone}: {resp!r}")
        return False
    return True


def _switch_off_lapsed_trials(db) -> int:
    """
    Trial expiry enforcement: access reads trial_ends_at live, but
    ai_enabled only flips on sync — so re-sync every merchant whose
    window lapsed and who still has Amara on anywhere. Merchants
    already fully off are skipped (sync would be a no-op). Returns the
    count switched off.
    """
    now = datetime.utcnow()
    switched_off = 0
    lapsed = (
        db.query(User)
        .filter(User.trial_ends_at.isnot(None), User.trial_ends_at <= now)
        .all()
    )
    for trial_user in lapsed:
        still_on = (
            db.query(WhatsAppAccount.id)
            .filter(
                WhatsAppAccount.user_id == trial_user.id,
                WhatsAppAccount.ai_enabled.is_(True),
            )
            .first()
            is not None
            or db.query(InstagramAccount.id)
            .filter(
                InstagramAccount.user_id == trial_user.id,
                InstagramAccount.ai_enabled.is_(True),
            )
            .first()
            is not None
        )
        if still_on:
            sync_ai_access_for_merchant(db, trial_user)
            switched_off += 1
    return switched_off


@celery_app.task
def send_subscription_nudges():
    """
    Daily sweep: remind connected-but-unpaid merchants that Amara is paused
    and link them to Settings where Pay Now lives. Targeting mirrors the
    access decision exactly (paid, in-flight, in-grace, plan sellers, and
    active trials are all skipped via is_merchant_allowed_ai), plus a
    connected channel and a phone number on file. Sends are recorded as
    SUBSCRIPTION_NUDGE events — the same rows drive the per-merchant
    schedule, so a double run can never double-nudge. Merchant-facing,
    like every other alert: never billable, no record_reply call.
    Also enforces trial expiry (runs before the nudge early-returns, so a
    missing template or credentials never blocks it).
    """
    db = SessionLocal()
    try:
        expired = _switch_off_lapsed_trials(db)
        if not settings.WHATSAPP_NUDGE_TEMPLATE_NAME:
            return f"Switched off {expired} lapsed trials. Nudge template not configured; skipping sends."
        if not settings.WHATSAPP_TOKEN or not settings.PHONE_NUMBER_ID:
            return f"Switched off {expired} lapsed trials. Platform WhatsApp credentials missing; skipping sends."
        if not get_platform_plans(db):
            # Gating itself is off — everyone is allowed, nobody to nudge.
            return f"Switched off {expired} lapsed trials. No platform plans configured; nothing to nudge."
        now = datetime.utcnow()
        sent, skipped = 0, 0
        for user in db.query(User).all():
            phone = user.username or user.phone_number
            if not phone:
                continue
            if is_merchant_allowed_ai(db, user):
                continue
            if not _has_connected_channel(db, user.id):
                continue
            if not _nudge_due(_nudge_history(db, user.id), now):
                continue
            if _send_nudge(phone):
                usage.record_event(
                    db,
                    user.id,
                    AmaraEventKind.SUBSCRIPTION_NUDGE,
                    channel="whatsapp",
                )
                sent += 1
            else:
                skipped += 1
        return f"Sent {sent} subscription nudges ({skipped} skipped on send failure)."
    finally:
        db.close()
