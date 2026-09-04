from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.db.models import PhoneNumberProvisioningAttempt, ProvisioningStatus

WEBHOOK_URL = "/api/v1/pool/twilio-sms-webhook"


def _configure_twilio(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.example.com")


def _signed_request(client, monkeypatch, params):
    _configure_twilio(monkeypatch)
    signed_url = f"{settings.PUBLIC_BASE_URL}{WEBHOOK_URL}"
    signature = RequestValidator(settings.TWILIO_AUTH_TOKEN).compute_signature(signed_url, params)
    return client.post(WEBHOOK_URL, data=params, headers={"X-Twilio-Signature": signature})


def test_webhook_not_configured_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)

    resp = client.post(WEBHOOK_URL, data={"To": "+12025551234", "Body": "123-456"})

    assert resp.status_code == 503


def test_webhook_rejects_bad_signature(client, monkeypatch):
    _configure_twilio(monkeypatch)

    resp = client.post(
        WEBHOOK_URL,
        data={"To": "+12025551234", "Body": "123-456"},
        headers={"X-Twilio-Signature": "not-the-real-signature"},
    )

    assert resp.status_code == 403


def test_webhook_rejects_missing_signature(client, monkeypatch):
    _configure_twilio(monkeypatch)

    resp = client.post(WEBHOOK_URL, data={"To": "+12025551234", "Body": "123-456"})

    assert resp.status_code == 403


def test_webhook_stores_code_on_matching_attempt(client, db_session, monkeypatch):
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.CODE_REQUESTED
    )
    db_session.add(attempt)
    db_session.commit()

    params = {
        "To": "+12025551234",
        "From": "+12345",
        "Body": "123-456 is your WhatsApp Business Platform verification code",
    }
    resp = _signed_request(client, monkeypatch, params)

    assert resp.status_code == 200
    db_session.refresh(attempt)
    assert attempt.verification_code == "123456"


def test_webhook_no_matching_attempt(client, db_session, monkeypatch):
    params = {"To": "+19999999999", "Body": "123-456 is your code"}

    resp = _signed_request(client, monkeypatch, params)

    assert resp.status_code == 200
    assert db_session.query(PhoneNumberProvisioningAttempt).count() == 0


def test_webhook_body_without_recognizable_code(client, db_session, monkeypatch):
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.CODE_REQUESTED
    )
    db_session.add(attempt)
    db_session.commit()

    params = {"To": "+12025551234", "Body": "Welcome to WhatsApp!"}
    resp = _signed_request(client, monkeypatch, params)

    assert resp.status_code == 200
    db_session.refresh(attempt)
    assert attempt.verification_code is None


def test_webhook_only_matches_code_requested_status(client, db_session, monkeypatch):
    # An attempt that already moved past CODE_REQUESTED shouldn't be
    # re-matched by a later, unrelated SMS to the same number.
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.COMPLETED
    )
    db_session.add(attempt)
    db_session.commit()

    params = {"To": "+12025551234", "Body": "123-456"}
    resp = _signed_request(client, monkeypatch, params)

    assert resp.status_code == 200
    db_session.refresh(attempt)
    assert attempt.verification_code is None
