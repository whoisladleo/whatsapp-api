import hashlib
import hmac

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


# --- generate_otp ------------------------------------------------------------

def test_generate_otp_default_length_and_numeric():
    otp = security.generate_otp()

    assert len(otp) == 6
    assert otp.isdigit()


def test_generate_otp_custom_length():
    otp = security.generate_otp(length=4)

    assert len(otp) == 4
    assert otp.isdigit()
