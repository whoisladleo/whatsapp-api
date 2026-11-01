import hashlib
import hmac
import json

import jwt
import pytest

from app.core import security
from app.core.config import settings


# --- verify_meta_signature -------------------------------------------------

def test_verify_meta_signature_valid(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    body = b'{"a": 1}'
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    assert security.verify_meta_signature(body, sig) is True


def test_verify_meta_signature_invalid(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    body = b'{"a": 1}'

    assert security.verify_meta_signature(body, "sha256=" + "0" * 64) is False


def test_verify_meta_signature_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")

    assert security.verify_meta_signature(b"body", None) is False


def test_verify_meta_signature_wrong_prefix(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    body = b"body"
    digest = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    assert security.verify_meta_signature(body, f"sha1={digest}") is False


def test_verify_meta_signature_tampered_body(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    sig = "sha256=" + hmac.new(b"topsecret", b"original", hashlib.sha256).hexdigest()

    assert security.verify_meta_signature(b"tampered", sig) is False


# --- verify_tunzaa_signature -------------------------------------------------

def test_verify_tunzaa_signature_valid(monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", "malipo-secret")
    payload = {"transaction_id": "T1", "status": "COMPLETED", "reference_id": "order-1"}
    canonical = json.dumps(payload, sort_keys=True)
    sig = hmac.new(b"malipo-secret", canonical.encode(), hashlib.sha256).hexdigest()

    assert security.verify_tunzaa_signature(payload, sig) is True


def test_verify_tunzaa_signature_key_order_independent(monkeypatch):
    """The signer may serialize keys in a different order than they were
    received in the dict — canonicalization must sort keys before signing,
    matching python's json.dumps(payload, sort_keys=True) per Tunzaa's docs."""
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", "malipo-secret")
    payload = {"status": "COMPLETED", "reference_id": "order-1", "transaction_id": "T1"}
    canonical = json.dumps(
        {"transaction_id": "T1", "status": "COMPLETED", "reference_id": "order-1"}, sort_keys=True
    )
    sig = hmac.new(b"malipo-secret", canonical.encode(), hashlib.sha256).hexdigest()

    assert security.verify_tunzaa_signature(payload, sig) is True


def test_verify_tunzaa_signature_invalid(monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", "malipo-secret")

    assert security.verify_tunzaa_signature({"a": 1}, "0" * 64) is False


def test_verify_tunzaa_signature_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", "malipo-secret")

    assert security.verify_tunzaa_signature({"a": 1}, None) is False


# --- is_safe_external_url ---------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://10.0.0.5/hook",
        "http://192.168.1.5/hook",
        "http://172.16.0.5/hook",
        "http://0.0.0.0/hook",
        "ftp://8.8.8.8/hook",  # non-http(s) scheme
        "not-a-url",
        "",
    ],
)
def test_is_safe_external_url_rejects_unsafe(url):
    assert security.is_safe_external_url(url) is False


def test_is_safe_external_url_accepts_public_ip_literal():
    # Using an IP literal avoids a real DNS lookup in the test.
    assert security.is_safe_external_url("https://8.8.8.8/callback") is True


def test_is_safe_external_url_returns_false_when_dns_resolution_fails(monkeypatch):
    def raise_gaierror(*args, **kwargs):
        raise OSError("DNS resolution failed")

    monkeypatch.setattr(security.socket, "getaddrinfo", raise_gaierror)

    assert security.is_safe_external_url("https://example.com/callback") is False


# --- JWT create/decode round-trip ------------------------------------------

def test_create_access_token_round_trip():
    token = security.create_access_token(subject=42)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    assert payload["sub"] == "42"
    assert "exp" in payload


def test_create_access_token_invalid_secret_fails_decode():
    token = security.create_access_token(subject=1)

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-secret", algorithms=[settings.ALGORITHM])


# --- sha256_hex / verify_sha256_hex / generate_refresh_token ----------------


def test_sha256_hex_matches_hashlib():
    assert security.sha256_hex("123456") == hashlib.sha256(b"123456").hexdigest()


def test_verify_sha256_hex_accepts_matching_candidate():
    stored = security.sha256_hex("123456")

    assert security.verify_sha256_hex(stored, "123456") is True
    assert security.verify_sha256_hex(stored, "000000") is False


def test_verify_sha256_hex_rejects_missing_stored_value():
    assert security.verify_sha256_hex(None, "123456") is False
    assert security.verify_sha256_hex("", "123456") is False


def test_verify_sha256_hex_uses_constant_time_compare(monkeypatch):
    seen = {}
    real_compare_digest = hmac.compare_digest

    def spy(a, b):
        seen["called"] = True
        return real_compare_digest(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)

    assert security.verify_sha256_hex(security.sha256_hex("123456"), "123456") is True
    assert seen.get("called") is True


def test_generate_refresh_token_is_opaque_and_unique():
    first = security.generate_refresh_token()
    second = security.generate_refresh_token()

    assert first != second
    assert "." not in first  # opaque bearer, not a JWT
    assert len(first) >= 43  # 256 bits in urlsafe base64


# --- generate_otp ------------------------------------------------------------

def test_generate_otp_default_length_and_numeric():
    otp = security.generate_otp()

    assert len(otp) == 6
    assert otp.isdigit()


def test_generate_otp_custom_length():
    otp = security.generate_otp(length=4)

    assert len(otp) == 4
    assert otp.isdigit()


# --- normalize_phone ---------------------------------------------------------

def test_normalize_phone_leading_zero_becomes_country_code():
    assert security.normalize_phone("0712345678") == "255712345678"


def test_normalize_phone_plus_prefix_stripped():
    assert security.normalize_phone("+255712345678") == "255712345678"


def test_normalize_phone_already_normalized_unchanged():
    assert security.normalize_phone("255712345678") == "255712345678"


def test_normalize_phone_strips_non_digit_separators():
    assert security.normalize_phone("+255 712 345 678") == "255712345678"


def test_normalize_phone_equivalent_formats_compare_equal():
    assert security.normalize_phone("0712345678") == security.normalize_phone(
        "+255712345678"
    )


def test_normalize_phone_none_and_empty_passthrough():
    assert security.normalize_phone(None) is None
    assert security.normalize_phone("") == ""
