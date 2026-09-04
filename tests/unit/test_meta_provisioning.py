import httpx
import pytest
import respx

from app.core.config import settings
from app.services import meta_provisioning
from app.services.meta_provisioning import UnsupportedCountryError, split_e164

WABA_URL_RE = r"https://graph\.facebook\.com/.+/phone_numbers$"
REQUEST_CODE_URL_RE = r"https://graph\.facebook\.com/.+/request_code$"
VERIFY_CODE_URL_RE = r"https://graph\.facebook\.com/.+/verify_code$"
REGISTER_URL_RE = r"https://graph\.facebook\.com/.+/register$"


@pytest.fixture(autouse=True)
def _pool_config(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_POOL_WABA_ID", "WABA123")
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "pool-token")


def test_split_e164_known_country():
    cc, national = split_e164("+12025551234", "US")
    assert cc == "1"
    assert national == "2025551234"


def test_split_e164_unsupported_country():
    with pytest.raises(UnsupportedCountryError):
        split_e164("+255700000001", "ZZ")


def test_split_e164_mismatched_calling_code():
    # A GB-formatted number passed off as a US one
    with pytest.raises(UnsupportedCountryError):
        split_e164("+447911123456", "US")


@respx.mock
async def test_add_phone_number_to_waba_success():
    route = respx.post(url__regex=WABA_URL_RE).mock(
        return_value=httpx.Response(200, json={"id": "PHONE_ID_1"})
    )

    phone_number_id = await meta_provisioning.add_phone_number_to_waba(
        "+12025551234", "US", "Taskflow OS"
    )

    assert phone_number_id == "PHONE_ID_1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer pool-token"


@respx.mock
async def test_add_phone_number_to_waba_rejected():
    respx.post(url__regex=WABA_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )

    with pytest.raises(RuntimeError):
        await meta_provisioning.add_phone_number_to_waba("+12025551234", "US", "Taskflow OS")


@respx.mock
async def test_request_verification_code_success():
    route = respx.post(url__regex=REQUEST_CODE_URL_RE).mock(return_value=httpx.Response(200, json={}))

    await meta_provisioning.request_verification_code("PHONE_ID_1")

    assert route.called


@respx.mock
async def test_request_verification_code_failure():
    respx.post(url__regex=REQUEST_CODE_URL_RE).mock(return_value=httpx.Response(400, json={}))

    with pytest.raises(RuntimeError):
        await meta_provisioning.request_verification_code("PHONE_ID_1")


@respx.mock
async def test_submit_verification_code_success():
    route = respx.post(url__regex=VERIFY_CODE_URL_RE).mock(return_value=httpx.Response(200, json={}))

    await meta_provisioning.submit_verification_code("PHONE_ID_1", "123456")

    assert route.called


@respx.mock
async def test_submit_verification_code_failure():
    respx.post(url__regex=VERIFY_CODE_URL_RE).mock(return_value=httpx.Response(400, json={}))

    with pytest.raises(RuntimeError):
        await meta_provisioning.submit_verification_code("PHONE_ID_1", "000000")


@respx.mock
async def test_register_for_messaging_success():
    route = respx.post(url__regex=REGISTER_URL_RE).mock(return_value=httpx.Response(200, json={}))

    await meta_provisioning.register_for_messaging("PHONE_ID_1", "123456")

    assert route.called


@respx.mock
async def test_register_for_messaging_failure():
    respx.post(url__regex=REGISTER_URL_RE).mock(return_value=httpx.Response(400, json={}))

    with pytest.raises(RuntimeError):
        await meta_provisioning.register_for_messaging("PHONE_ID_1", "123456")
