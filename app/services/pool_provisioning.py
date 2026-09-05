import asyncio
import logging
import secrets
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    PhoneNumberPool,
    PhoneNumberProvisioningAttempt,
    PhoneNumberStatus,
    ProvisioningStatus,
)
from app.services import meta_provisioning
from app.services.twilio_provisioning import TwilioProvisioningService

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 120


class PoolProvisioningError(Exception):
    """
    Raised when a provisioning run fails at any step. The attempt row
    (attached as `.attempt`) records exactly which step it reached and
    `error_message` — check that rather than parsing this exception's text.
    `.attempt` is None only for a failure before any row could be created
    (the Twilio purchase itself failing — see provision_pool_number).
    """

    def __init__(self, message: str, attempt: Optional[PhoneNumberProvisioningAttempt]):
        super().__init__(message)
        self.attempt = attempt


def _fail(
    db: Session,
    attempt: PhoneNumberProvisioningAttempt,
    twilio_service: TwilioProvisioningService,
    error: str,
) -> None:
    attempt.status = ProvisioningStatus.FAILED
    attempt.error_message = error
    db.commit()
    # Best-effort — a purchased-but-unusable number shouldn't sit around
    # still being billed just because a later Meta step failed.
    if attempt.twilio_sid:
        twilio_service.release_number(attempt.twilio_sid)
    raise PoolProvisioningError(error, attempt)


async def _poll_for_code(db: Session, attempt: PhoneNumberProvisioningAttempt) -> Optional[str]:
    """
    Meta's verification code arrives asynchronously via the Twilio SMS
    webhook (app/api/v1/pool.py), which runs in a separate request/process
    from this one — polling the DB is how this run picks it up.
    """
    elapsed = 0
    while elapsed < POLL_TIMEOUT_SECONDS:
        db.refresh(attempt)
        if attempt.verification_code:
            return attempt.verification_code
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
    return None


async def provision_pool_number(
    db: Session,
    country_code: Optional[str] = None,
    verified_name: str = "Taskflow OS",
) -> PhoneNumberPool:
    """
    Fully automated replacement for the old "add a number in Meta Business
    Manager by hand, verify by hand, run scripts/add_pool_number.py" flow.
    Buys a real number from Twilio, registers + verifies it against
    Taskflow's pool WABA, and inserts it into PhoneNumberPool as AVAILABLE.

    verified_name defaults to a generic placeholder for ops-driven pool
    replenishment, where the real merchant isn't known yet (the real name
    gets requested later, at assignment time, via the existing
    request_display_name_change in app/api/v1/accounts.py). Callers doing
    an on-demand purchase for a specific merchant right now (the
    reassign_from_pool fallback in app/api/v1/accounts.py) should pass that
    merchant's actual account name instead, so it's correct from the start.
    """
    country_code = country_code or settings.TWILIO_COUNTRY_CODE
    if not settings.PUBLIC_BASE_URL:
        raise ValueError("PUBLIC_BASE_URL is not configured — Twilio would have no way to reach our webhook.")
    if not settings.WHATSAPP_POOL_WABA_ID or not settings.WHATSAPP_POOL_ACCESS_TOKEN:
        raise ValueError("WHATSAPP_POOL_WABA_ID/WHATSAPP_POOL_ACCESS_TOKEN are not configured.")
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise ValueError("TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN are not configured.")

    twilio_service = TwilioProvisioningService()
    webhook_url = f"{settings.PUBLIC_BASE_URL}/api/v1/pool/twilio-sms-webhook"

    try:
        purchased = twilio_service.purchase_number(country_code, webhook_url)
    except Exception as e:
        # No attempt row exists yet at this point — nothing purchased,
        # nothing to release, nothing to mark FAILED. Still wrap it so
        # callers only ever need to handle PoolProvisioningError, not also
        # every TwilioProvisioningService exception type directly.
        raise PoolProvisioningError(f"purchase_number failed: {e}", attempt=None)

    attempt = PhoneNumberProvisioningAttempt(
        twilio_sid=purchased["sid"],
        phone_number=purchased["phone_number"],
        status=ProvisioningStatus.PURCHASED,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    logger.info(f"Provisioning attempt {attempt.id}: purchased {attempt.phone_number} ({attempt.twilio_sid})")

    try:
        phone_number_id = await meta_provisioning.add_phone_number_to_waba(
            attempt.phone_number, country_code, verified_name
        )
    except Exception as e:
        _fail(db, attempt, twilio_service, f"add_phone_number_to_waba failed: {e}")

    attempt.phone_number_id = phone_number_id
    attempt.status = ProvisioningStatus.META_REGISTERED
    db.commit()

    try:
        await meta_provisioning.request_verification_code(phone_number_id)
    except Exception as e:
        _fail(db, attempt, twilio_service, f"request_verification_code failed: {e}")

    attempt.status = ProvisioningStatus.CODE_REQUESTED
    db.commit()

    code = await _poll_for_code(db, attempt)
    if not code:
        _fail(db, attempt, twilio_service, "Timed out waiting for Meta's verification SMS.")

    try:
        await meta_provisioning.submit_verification_code(phone_number_id, code)
    except Exception as e:
        _fail(db, attempt, twilio_service, f"submit_verification_code failed: {e}")

    attempt.status = ProvisioningStatus.VERIFIED
    db.commit()

    # A stable secret we control, not something a person needs to remember —
    # only used again if this number's display name changes later and Meta
    # requires re-registration (see request_display_name_change).
    pin = f"{secrets.randbelow(1_000_000):06d}"
    try:
        await meta_provisioning.register_for_messaging(phone_number_id, pin)
    except Exception as e:
        _fail(db, attempt, twilio_service, f"register_for_messaging failed: {e}")

    attempt.two_step_pin = pin
    attempt.status = ProvisioningStatus.REGISTERED
    db.commit()

    pool_number = PhoneNumberPool(
        phone_number_id=phone_number_id,
        display_name=f"Twilio {attempt.phone_number}",
        two_step_pin=pin,
        status=PhoneNumberStatus.AVAILABLE,
    )
    db.add(pool_number)
    attempt.status = ProvisioningStatus.COMPLETED
    db.commit()
    db.refresh(pool_number)
    logger.info(f"Provisioning attempt {attempt.id}: completed, phone_number_id={phone_number_id}")
    return pool_number
