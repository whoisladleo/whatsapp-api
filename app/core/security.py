from datetime import datetime, timedelta
from typing import Any, Optional, Union
import hashlib
import hmac
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse
import jwt
import secrets
from app.core.config import settings


def create_access_token(
    subject: Union[str, Any], expires_delta: timedelta = None
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def generate_otp(length: int = 6) -> str:
    """Generates a secure numeric OTP of the specified length."""
    return ''.join(str(secrets.randbelow(10)) for _ in range(length))


def verify_meta_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verifies Meta's X-Hub-Signature-256 header against the raw webhook body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def verify_tunzaa_signature(payload: dict, signature_header: Optional[str]) -> bool:
    """
    Verifies Tunzaa/Malipo's X-Signature header: HMAC-SHA256 of the JSON
    payload re-serialized with alphabetically sorted keys (equivalent to
    Python's json.dumps(payload, sort_keys=True), which is exactly what this
    uses) against settings.TUNZAA_SECRET_KEY. Per Tunzaa's own webhook docs.
    """
    if not signature_header:
        return False
    canonical = json.dumps(payload, sort_keys=True)
    expected = hmac.new(
        settings.TUNZAA_SECRET_KEY.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """
    Normalizes a phone number for comparison purposes only — this does not
    change how numbers are stored anywhere, it's meant to be applied to both
    sides of a comparison so "0712..." and "255712..." (the two formats this
    codebase's WhatsApp-supplied numbers and manually-entered numbers most
    commonly disagree on for Tanzanian numbers) compare equal. Strips all
    non-digit characters, then collapses a leading local trunk-prefix "0"
    into the "255" country code.
    """
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = "255" + digits[1:]
    return digits


def is_safe_external_url(url: str) -> bool:
    """
    Rejects URLs pointing at loopback/private/link-local/reserved addresses,
    to guard outbound requests (e.g. account callback_url forwarding) against SSRF.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for family, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
        return True
    except Exception:
        return False
