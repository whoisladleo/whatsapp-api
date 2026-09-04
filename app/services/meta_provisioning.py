import logging
import re
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Meta's POST /{waba_id}/phone_numbers wants the calling code and national
# number as separate fields, but Twilio hands back a plain E.164 string
# (e.g. "+12025551234") — this maps the handful of countries
# TWILIO_COUNTRY_CODE realistically gets set to today. Extend this before
# provisioning pool numbers in a country it doesn't cover; there's no
# reliable way to split calling code from national number without either a
# lookup table like this or a full library (e.g. `phonenumbers`), which
# isn't otherwise a dependency here.
CALLING_CODE_BY_COUNTRY = {
    "US": "1",
    "CA": "1",
    "GB": "44",
    "TZ": "255",
    "KE": "254",
    "UG": "256",
    "NG": "234",
    "ZA": "27",
}


class UnsupportedCountryError(Exception):
    """Raised when CALLING_CODE_BY_COUNTRY has no entry for the configured country."""


def split_e164(phone_number_e164: str, country_code: str) -> tuple[str, str]:
    """Returns (calling_code, national_number) for Meta's `cc`/`phone_number` fields."""
    calling_code = CALLING_CODE_BY_COUNTRY.get(country_code.upper())
    if not calling_code:
        raise UnsupportedCountryError(
            f"No calling code mapping for {country_code!r} — add one to CALLING_CODE_BY_COUNTRY."
        )
    digits = re.sub(r"\D", "", phone_number_e164)
    if not digits.startswith(calling_code):
        raise UnsupportedCountryError(
            f"{phone_number_e164!r} doesn't start with calling code {calling_code!r} for {country_code!r}."
        )
    return calling_code, digits[len(calling_code):]


def _pool_headers() -> dict:
    return {"Authorization": f"Bearer {settings.WHATSAPP_POOL_ACCESS_TOKEN}"}


async def add_phone_number_to_waba(
    phone_number_e164: str, country_code: str, verified_name: str
) -> str:
    """
    Registers a real phone number onto Taskflow's pool WABA
    (settings.WHATSAPP_POOL_WABA_ID) via POST /{waba_id}/phone_numbers.
    Returns the new phone_number_id Meta assigns.
    """
    cc, national_number = split_e164(phone_number_e164, country_code)
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{settings.WHATSAPP_POOL_WABA_ID}/phone_numbers"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers=_pool_headers(),
            data={"cc": cc, "phone_number": national_number, "verified_name": verified_name},
        )
    data = response.json()
    if response.status_code != 200 or not data.get("id"):
        raise RuntimeError(f"Meta rejected add_phone_number_to_waba: {data}")
    return data["id"]


async def request_verification_code(phone_number_id: str, code_method: str = "SMS") -> None:
    """POST /{phone_number_id}/request_code — triggers Meta to text/call the number with a code."""
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}/request_code"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, headers=_pool_headers(), json={"code_method": code_method, "language": "en"}
        )
    if response.status_code != 200:
        raise RuntimeError(f"Meta rejected request_verification_code: {response.json()}")


async def submit_verification_code(phone_number_id: str, code: str) -> None:
    """POST /{phone_number_id}/verify_code — completes ownership verification of the number."""
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}/verify_code"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=_pool_headers(), json={"code": code})
    if response.status_code != 200:
        raise RuntimeError(f"Meta rejected submit_verification_code: {response.json()}")


async def register_for_messaging(phone_number_id: str, pin: str) -> None:
    """POST /{phone_number_id}/register — activates the (now-verified) number for Cloud API messaging."""
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}/register"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, headers=_pool_headers(), json={"messaging_product": "whatsapp", "pin": pin}
        )
    if response.status_code != 200:
        raise RuntimeError(f"Meta rejected register_for_messaging: {response.json()}")
