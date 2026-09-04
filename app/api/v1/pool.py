import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.db.models import PhoneNumberProvisioningAttempt, ProvisioningStatus
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# Meta's WhatsApp verification SMS body is a 6-digit code, sometimes
# hyphenated in the middle (e.g. "123-456 is your WhatsApp Business
# Platform verification code") — pull the digits out regardless of exact
# wording rather than matching the surrounding sentence.
_CODE_RE = re.compile(r"\b(\d{3})-?(\d{3})\b")


@router.post("/twilio-sms-webhook")
async def twilio_sms_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_twilio_signature: str = Header(None, alias="X-Twilio-Signature"),
):
    """
    Twilio calls this (configured as the purchased number's inbound-SMS
    webhook — see TwilioProvisioningService.purchase_number) when Meta's
    verification SMS arrives during automated pool-number provisioning
    (app/services/pool_provisioning.py). Only stores the code against the
    matching in-progress attempt; submitting it to Meta happens in the
    provisioning script's poll loop, not here.
    """
    if not settings.TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio is not configured.")

    form = await request.form()
    params = dict(form)

    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    # Twilio signs the exact URL it called — if this service sits behind a
    # proxy, PUBLIC_BASE_URL (not request.url, which may reflect an internal
    # scheme/host) must match what was actually configured as the webhook.
    signed_url = f"{settings.PUBLIC_BASE_URL}/api/v1/pool/twilio-sms-webhook"
    if not x_twilio_signature or not validator.validate(signed_url, params, x_twilio_signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    to_number = params.get("To")
    body = params.get("Body", "")

    attempt = (
        db.query(PhoneNumberProvisioningAttempt)
        .filter(
            PhoneNumberProvisioningAttempt.phone_number == to_number,
            PhoneNumberProvisioningAttempt.status == ProvisioningStatus.CODE_REQUESTED,
        )
        .order_by(PhoneNumberProvisioningAttempt.id.desc())
        .first()
    )
    if not attempt:
        logger.warning(f"Twilio SMS to {to_number} with no matching in-progress provisioning attempt.")
        return Response(content="<Response></Response>", media_type="application/xml")

    match = _CODE_RE.search(body)
    if match:
        attempt.verification_code = f"{match.group(1)}{match.group(2)}"
        db.commit()
    else:
        logger.warning(f"Twilio SMS to {to_number} didn't contain a recognizable code: {body!r}")

    return Response(content="<Response></Response>", media_type="application/xml")
