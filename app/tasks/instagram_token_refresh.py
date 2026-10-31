import logging
from datetime import datetime, timedelta

from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.crud import instagram as crud_instagram
from app.db.models import InstagramAccount
from app.db.session import SessionLocal
from app.services import instagram_oauth
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def refresh_account_token(account_id: str):
    """
    Refreshes one Instagram account's long-lived token before its ~60-day
    expiry (#80). Dispatched lazily from the webhook handlers when
    token_needs_refresh says stale — there's no Celery Beat, so there's no
    periodic runner; any account active enough to matter gets inbound
    traffic inside the window. Dormant 60+ days: the token dies and the
    merchant must reconnect (a documented limit, not a silent one — the
    failure is logged on every attempt).
    """
    db = SessionLocal()
    try:
        return _run_async(_refresh(db, account_id))
    finally:
        db.close()


async def _refresh(db, account_id: str) -> str:
    account_uuid = coerce_uuid(account_id)
    if account_uuid is None:
        return "Account not found."
    account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.id == account_uuid)
        .first()
    )
    if not account or not account.access_token:
        return "Account gone or disconnected."
    if not crud_instagram.token_needs_refresh(account):
        return "Token fresh; skipping."

    old_stamp = account.token_refreshed_at
    cutoff = datetime.utcnow() - timedelta(
        days=crud_instagram.TOKEN_REFRESH_AFTER_DAYS
    )
    # Claim: concurrent webhooks dispatch concurrent refreshes, but Meta
    # rate-limits refresh and the old token may invalidate — exactly one
    # may call the API. Same conditional-UPDATE style as the DM claim.
    claimed = (
        db.query(InstagramAccount)
        .filter(
            InstagramAccount.id == account_uuid,
            (
                (InstagramAccount.token_refreshed_at.is_(None))
                | (InstagramAccount.token_refreshed_at < cutoff)
            ),
        )
        .update(
            {InstagramAccount.token_refreshed_at: datetime.utcnow()},
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed == 0:
        return "Already refreshed; skipping duplicate dispatch."

    try:
        new_token = await instagram_oauth.refresh_long_lived_token(
            account.access_token
        )
    except Exception as e:
        # Restore the old stamp so the next inbound retries — a failed
        # refresh must not look freshly refreshed for 50 days.
        db.query(InstagramAccount).filter(
            InstagramAccount.id == account_uuid
        ).update(
            {InstagramAccount.token_refreshed_at: old_stamp},
            synchronize_session=False,
        )
        db.commit()
        logger.warning("Token refresh failed for account %s: %s", account_uuid, e)
        return "Refresh failed; stamp restored."

    db.query(InstagramAccount).filter(
        InstagramAccount.id == account_uuid
    ).update(
        {InstagramAccount.access_token: new_token},
        synchronize_session=False,
    )
    db.commit()
    return "Refreshed."
