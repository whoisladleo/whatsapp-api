import logging
from typing import Optional

from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)


class TwilioNotConfiguredError(Exception):
    """Raised when TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN aren't set."""


class NoAvailableTwilioNumberError(Exception):
    """Raised when Twilio has no SMS+voice-capable local number left to buy in the target country."""


class TwilioProvisioningService:
    """
    Thin wrapper around the Twilio numbers API for automated pool
    provisioning (see app/services/pool_provisioning.py). Twilio is the
    source of the phone number itself and of receiving Meta's SMS
    verification code — registering that number for WhatsApp messaging is a
    separate, later step against Meta's Graph API
    (app/services/meta_provisioning.py).
    """

    def __init__(self, account_sid: Optional[str] = None, auth_token: Optional[str] = None):
        self.account_sid = account_sid or settings.TWILIO_ACCOUNT_SID
        self.auth_token = auth_token or settings.TWILIO_AUTH_TOKEN
        self._client: Optional[Client] = None

    @property
    def client(self) -> Client:
        if not self.account_sid or not self.auth_token:
            raise TwilioNotConfiguredError()
        if self._client is None:
            self._client = Client(self.account_sid, self.auth_token)
        return self._client

    def purchase_number(self, country_code: str, sms_webhook_url: str) -> dict:
        """
        Buys one SMS+voice-capable local number in `country_code` and points
        its inbound-SMS webhook at `sms_webhook_url` (must be publicly
        reachable — see settings.PUBLIC_BASE_URL) so Meta's verification
        code lands there. Returns {"sid": ..., "phone_number": ...}.
        """
        candidates = self.client.available_phone_numbers(country_code).local.list(
            sms_enabled=True, voice_enabled=True, limit=1
        )
        if not candidates:
            raise NoAvailableTwilioNumberError(
                f"No SMS+voice-capable Twilio numbers available in {country_code!r}."
            )

        purchased = self.client.incoming_phone_numbers.create(
            phone_number=candidates[0].phone_number,
            sms_url=sms_webhook_url,
            sms_method="POST",
        )
        return {"sid": purchased.sid, "phone_number": purchased.phone_number}

    def release_number(self, twilio_sid: str) -> None:
        """
        Best-effort release of a purchased number — called when a
        provisioning run fails after the Twilio purchase step, so a failure
        doesn't leave an orphaned, still-billed number behind. Failures here
        are logged, not raised: the provisioning run is already failing for
        its own reason, and that's the one that should surface.
        """
        try:
            self.client.incoming_phone_numbers(twilio_sid).delete()
        except Exception as e:
            logger.error(f"Failed to release Twilio number {twilio_sid}: {e}")
