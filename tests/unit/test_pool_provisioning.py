import pytest

from app.core.config import settings
from app.db.models import PhoneNumberPool, PhoneNumberProvisioningAttempt, ProvisioningStatus
from app.services import meta_provisioning, pool_provisioning
from app.services.pool_provisioning import PoolProvisioningError, _poll_for_code, provision_pool_number


class _FakeTwilioService:
    def __init__(self, purchase_result=None):
        self._purchase_result = purchase_result or {"sid": "PN_SID_1", "phone_number": "+12025551234"}
        self.released_sids = []

    def purchase_number(self, country_code, sms_webhook_url):
        return self._purchase_result

    def release_number(self, twilio_sid):
        self.released_sids.append(twilio_sid)


@pytest.fixture(autouse=True)
def _pool_config(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setattr(settings, "WHATSAPP_POOL_WABA_ID", "WABA123")
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "pool-token")
    # TwilioProvisioningService itself gets faked out per-test (see
    # fake_twilio / individual monkeypatches below) — this just needs to
    # pass provision_pool_number's own upfront "is Twilio configured" check.
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "tok_fake")


@pytest.fixture
def fake_twilio(monkeypatch):
    service = _FakeTwilioService()
    monkeypatch.setattr(pool_provisioning, "TwilioProvisioningService", lambda: service)
    return service


def _patch_meta_happy_path(monkeypatch):
    async def _add(*a, **kw):
        return "PHONE_ID_1"

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(meta_provisioning, "add_phone_number_to_waba", _add)
    monkeypatch.setattr(meta_provisioning, "request_verification_code", _noop)
    monkeypatch.setattr(meta_provisioning, "submit_verification_code", _noop)
    monkeypatch.setattr(meta_provisioning, "register_for_messaging", _noop)


async def test_provision_pool_number_happy_path(db_session, fake_twilio, monkeypatch):
    _patch_meta_happy_path(monkeypatch)

    async def _instant_code(db, attempt):
        return "123456"

    monkeypatch.setattr(pool_provisioning, "_poll_for_code", _instant_code)

    pool_number = await provision_pool_number(db_session, country_code="US")

    assert pool_number.phone_number_id == "PHONE_ID_1"
    assert pool_number.status.value == "AVAILABLE"
    assert pool_number.two_step_pin and len(pool_number.two_step_pin) == 6

    attempt = db_session.query(PhoneNumberProvisioningAttempt).one()
    assert attempt.status == ProvisioningStatus.COMPLETED
    assert attempt.phone_number == "+12025551234"
    assert attempt.twilio_sid == "PN_SID_1"
    assert fake_twilio.released_sids == []  # never released on success


async def test_provision_pool_number_fails_purchase(db_session, monkeypatch):
    class _FailingTwilioService:
        def purchase_number(self, country_code, sms_webhook_url):
            raise RuntimeError("Twilio account suspended")

    monkeypatch.setattr(pool_provisioning, "TwilioProvisioningService", lambda: _FailingTwilioService())

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert exc_info.value.attempt is None
    assert "purchase_number failed" in str(exc_info.value)
    assert db_session.query(PhoneNumberProvisioningAttempt).count() == 0


async def test_provision_pool_number_missing_public_base_url(db_session, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)

    with pytest.raises(ValueError):
        await provision_pool_number(db_session)


async def test_provision_pool_number_missing_waba_config(db_session, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_POOL_WABA_ID", None)

    with pytest.raises(ValueError):
        await provision_pool_number(db_session)


async def test_provision_pool_number_missing_twilio_config(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)

    with pytest.raises(ValueError):
        await provision_pool_number(db_session)


async def test_provision_pool_number_fails_add_to_waba(db_session, fake_twilio, monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("Meta said no")

    monkeypatch.setattr(meta_provisioning, "add_phone_number_to_waba", _boom)

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert exc_info.value.attempt.status == ProvisioningStatus.FAILED
    assert "add_phone_number_to_waba failed" in exc_info.value.attempt.error_message
    assert fake_twilio.released_sids == ["PN_SID_1"]
    assert db_session.query(PhoneNumberPool).count() == 0


async def test_provision_pool_number_fails_request_code(db_session, fake_twilio, monkeypatch):
    async def _add(*a, **kw):
        return "PHONE_ID_1"

    async def _boom(*a, **kw):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(meta_provisioning, "add_phone_number_to_waba", _add)
    monkeypatch.setattr(meta_provisioning, "request_verification_code", _boom)

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert "request_verification_code failed" in exc_info.value.attempt.error_message
    assert fake_twilio.released_sids == ["PN_SID_1"]


async def test_provision_pool_number_times_out_waiting_for_code(db_session, fake_twilio, monkeypatch):
    _patch_meta_happy_path(monkeypatch)

    async def _never(db, attempt):
        return None

    monkeypatch.setattr(pool_provisioning, "_poll_for_code", _never)

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert "Timed out" in exc_info.value.attempt.error_message
    assert fake_twilio.released_sids == ["PN_SID_1"]


async def test_provision_pool_number_fails_verify_code(db_session, fake_twilio, monkeypatch):
    _patch_meta_happy_path(monkeypatch)

    async def _instant_code(db, attempt):
        return "123456"

    async def _boom(*a, **kw):
        raise RuntimeError("wrong code")

    monkeypatch.setattr(pool_provisioning, "_poll_for_code", _instant_code)
    monkeypatch.setattr(meta_provisioning, "submit_verification_code", _boom)

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert "submit_verification_code failed" in exc_info.value.attempt.error_message
    assert fake_twilio.released_sids == ["PN_SID_1"]


async def test_provision_pool_number_fails_register(db_session, fake_twilio, monkeypatch):
    _patch_meta_happy_path(monkeypatch)

    async def _instant_code(db, attempt):
        return "123456"

    async def _boom(*a, **kw):
        raise RuntimeError("register failed")

    monkeypatch.setattr(pool_provisioning, "_poll_for_code", _instant_code)
    monkeypatch.setattr(meta_provisioning, "register_for_messaging", _boom)

    with pytest.raises(PoolProvisioningError) as exc_info:
        await provision_pool_number(db_session, country_code="US")

    assert "register_for_messaging failed" in exc_info.value.attempt.error_message
    assert fake_twilio.released_sids == ["PN_SID_1"]
    assert db_session.query(PhoneNumberPool).count() == 0


async def test_poll_for_code_finds_code(db_session, monkeypatch):
    monkeypatch.setattr(pool_provisioning, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(pool_provisioning, "POLL_TIMEOUT_SECONDS", 1)
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.CODE_REQUESTED, verification_code="654321"
    )
    db_session.add(attempt)
    db_session.commit()

    code = await _poll_for_code(db_session, attempt)

    assert code == "654321"


async def test_poll_for_code_finds_code_after_one_wait(db_session, monkeypatch):
    # Code isn't there on the first check — only test path that actually
    # exercises the sleep-and-recheck loop body, not just its first pass.
    monkeypatch.setattr(pool_provisioning, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(pool_provisioning, "POLL_TIMEOUT_SECONDS", 10)
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.CODE_REQUESTED
    )
    db_session.add(attempt)
    db_session.commit()

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        attempt.verification_code = "999999"
        db_session.commit()

    monkeypatch.setattr(pool_provisioning.asyncio, "sleep", _fake_sleep)

    code = await _poll_for_code(db_session, attempt)

    assert code == "999999"
    assert sleep_calls == [0]


async def test_poll_for_code_times_out(db_session, monkeypatch):
    monkeypatch.setattr(pool_provisioning, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(pool_provisioning, "POLL_TIMEOUT_SECONDS", 0)
    attempt = PhoneNumberProvisioningAttempt(
        phone_number="+12025551234", status=ProvisioningStatus.CODE_REQUESTED
    )
    db_session.add(attempt)
    db_session.commit()

    code = await _poll_for_code(db_session, attempt)

    assert code is None
