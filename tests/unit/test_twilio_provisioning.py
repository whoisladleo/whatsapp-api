import pytest

from app.core.config import settings
from app.services.twilio_provisioning import (
    NoAvailableTwilioNumberError,
    TwilioNotConfiguredError,
    TwilioProvisioningService,
)


class _FakeNumber:
    def __init__(self, phone_number):
        self.phone_number = phone_number


class _FakePurchased:
    def __init__(self, sid, phone_number):
        self.sid = sid
        self.phone_number = phone_number


class _FakeLocalList:
    def __init__(self, candidates):
        self._candidates = candidates

    def list(self, **kwargs):
        return self._candidates


class _FakeAvailableNumbers:
    def __init__(self, candidates):
        self.local = _FakeLocalList(candidates)


class _FakeIncomingNumberResource:
    """Returned by both `.incoming_phone_numbers.create(...)` (a purchase) and
    `.incoming_phone_numbers(sid)` (a handle for further calls, e.g. delete)."""

    def __init__(self, sid=None, phone_number=None):
        self.sid = sid
        self.phone_number = phone_number
        self.deleted = False

    def create(self, phone_number, sms_url, sms_method):
        return _FakePurchased(sid="PN_SID_1", phone_number=phone_number)

    def delete(self):
        self.deleted = True


class _FakeIncomingNumbers:
    """`client.incoming_phone_numbers` is both callable (with a sid, to get a
    resource handle) and has a `.create` method directly — mirrors Twilio's
    real API surface closely enough for this service's needs."""

    def __init__(self):
        self._resource = _FakeIncomingNumberResource()
        self.deleted_sids = []

    def create(self, phone_number, sms_url, sms_method):
        return self._resource.create(phone_number, sms_url, sms_method)

    def __call__(self, sid):
        handle = _FakeIncomingNumberResource(sid=sid)

        def _delete():
            self.deleted_sids.append(sid)

        handle.delete = _delete
        return handle


class _FakeClient:
    def __init__(self, candidates):
        self._candidates = candidates
        self.incoming_phone_numbers = _FakeIncomingNumbers()

    def available_phone_numbers(self, country_code):
        return _FakeAvailableNumbers(self._candidates)


def _service_with_fake_client(candidates):
    service = TwilioProvisioningService(account_sid="AC123", auth_token="tok")
    fake_client = _FakeClient(candidates)
    service._client = fake_client
    return service, fake_client


def test_client_property_raises_when_not_configured(monkeypatch):
    # Explicit None args alone aren't enough to prove this — __init__ falls
    # back to settings.TWILIO_ACCOUNT_SID/AUTH_TOKEN, which can be genuinely
    # configured in the environment this suite happens to run in.
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    service = TwilioProvisioningService(account_sid=None, auth_token=None)

    with pytest.raises(TwilioNotConfiguredError):
        _ = service.client


def test_client_property_constructs_and_caches_real_client():
    # twilio.rest.Client(sid, token) does no network I/O on construction —
    # safe to actually build here, just to cover the lazy-init branch.
    service = TwilioProvisioningService(account_sid="AC_fake", auth_token="tok_fake")

    first = service.client
    second = service.client

    assert first is second
    assert first.username == "AC_fake"


def test_purchase_number_success():
    service, fake_client = _service_with_fake_client([_FakeNumber("+12025551234")])

    result = service.purchase_number("US", "https://example.com/webhook")

    assert result == {"sid": "PN_SID_1", "phone_number": "+12025551234"}


def test_purchase_number_no_candidates():
    service, _ = _service_with_fake_client([])

    with pytest.raises(NoAvailableTwilioNumberError):
        service.purchase_number("US", "https://example.com/webhook")


def test_release_number_success():
    service, fake_client = _service_with_fake_client([])

    service.release_number("PN_SID_1")

    assert fake_client.incoming_phone_numbers.deleted_sids == ["PN_SID_1"]


def test_release_number_swallows_errors():
    service, fake_client = _service_with_fake_client([])

    def _boom(sid):
        raise RuntimeError("Twilio API down")

    fake_client.incoming_phone_numbers = _boom
    # Should log, not raise — a failed release shouldn't mask the original
    # provisioning failure that triggered it.
    service.release_number("PN_SID_1")
