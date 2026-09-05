import httpx
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import PhoneNumberPool, User
from app.services import pool_provisioning
from app.services.pool_provisioning import PoolProvisioningError

BASE = "/api/v1/accounts"
VALIDATE_URL_RE = r"https://graph\.facebook\.com/.+/PHONE1$"


@pytest.fixture
def other_user_headers(db_session):
    other = User(username="255700000040")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return {"Authorization": f"Bearer {create_access_token(other.id)}"}


def _account_payload(**overrides):
    payload = {
        "name": "My Shop",
        "phone_number_id": "PHONE1",
        "access_token": "meta-token",
    }
    payload.update(overrides)
    return payload


@respx.mock
def test_create_account_validates_credentials_and_persists(client, auth_user):
    _, headers = auth_user
    route = respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 201
    assert route.called
    body = resp.json()
    assert body["phone_number_id"] == "PHONE1"
    assert "access_token" not in body


@respx.mock
def test_create_account_rejects_invalid_credentials(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(401))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 400


@respx.mock
def test_create_account_rejects_when_validation_request_errors(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(side_effect=httpx.ConnectError("boom"))

    resp = client.post(BASE, json=_account_payload(), headers=headers)

    assert resp.status_code == 400


def test_create_account_requires_auth(client):
    resp = client.post(BASE, json=_account_payload())

    assert resp.status_code in (401, 403)


@respx.mock
def test_list_accounts_scoped_to_current_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE2$").mock(
        return_value=httpx.Response(200)
    )

    client.post(BASE, json=_account_payload(name="Mine"), headers=headers)
    client.post(
        BASE,
        json=_account_payload(name="Theirs", phone_number_id="PHONE2"),
        headers=other_user_headers,
    )

    resp = client.get(BASE, headers=headers)

    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == ["Mine"]


@respx.mock
def test_update_account_without_credential_change_skips_validation(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"name": "Renamed"}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


@respx.mock
def test_update_account_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.put(
        f"{BASE}/{created['id']}", json={"name": "Hijacked"}, headers=other_user_headers
    )

    assert resp.status_code == 404


@respx.mock
def test_update_account_revalidates_when_token_changes(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    revalidate_route = respx.get(url__regex=VALIDATE_URL_RE).mock(
        return_value=httpx.Response(401)
    )

    resp = client.put(
        f"{BASE}/{created['id']}",
        json={"access_token": "new-bad-token"},
        headers=headers,
    )

    assert resp.status_code == 400
    assert revalidate_route.called


@respx.mock
def test_update_account_reassign_from_pool_success(client, auth_user, db_session, monkeypatch):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    created = client.post(BASE, json=_account_payload(), headers=headers).json()

    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")
    db_session.add(PhoneNumberPool(phone_number_id="POOL_REASSIGN_1"))
    db_session.commit()
    # request_display_name_change (best-effort, swallows failures) fires
    # against this new number afterward — no route registered for it is
    # fine, same pattern test_create_account_auto_assigns_from_pool... relies
    # on: respx's unmocked-request error gets caught right there and ignored.

    resp = client.put(
        f"{BASE}/{created['id']}", json={"reassign_from_pool": True}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["phone_number_id"] == "POOL_REASSIGN_1"


def test_update_account_reassign_from_pool_exhausted_and_purchase_not_configured_returns_503(
    client, auth_user, monkeypatch
):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    # Pool empty, and TWILIO_ACCOUNT_SID etc. aren't set (conftest's
    # _no_real_external_credentials fixture already forces this) — so the
    # live-purchase fallback can't even start.
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")

    resp = client.put(
        f"{BASE}/{created['id']}", json={"reassign_from_pool": True}, headers=headers
    )

    assert resp.status_code == 503
    assert "purchasing a new one isn't configured" in resp.json()["detail"]


def test_update_account_reassign_from_pool_exhausted_purchases_new_number(
    client, auth_user, db_session, monkeypatch
):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")

    async def _fake_provision(db, verified_name=None, country_code=None):
        db.add(PhoneNumberPool(phone_number_id="FRESH_TWILIO_NUMBER"))
        db.commit()

    monkeypatch.setattr(pool_provisioning, "provision_pool_number", _fake_provision)

    with respx.mock:
        # request_display_name_change fires against the newly-purchased
        # number afterward — unmocked-and-swallowed, same as the plain
        # reassign-success test above.
        resp = client.put(
            f"{BASE}/{created['id']}", json={"reassign_from_pool": True}, headers=headers
        )

    assert resp.status_code == 200
    assert resp.json()["phone_number_id"] == "FRESH_TWILIO_NUMBER"


def test_update_account_reassign_from_pool_exhausted_purchase_fails_returns_502(
    client, auth_user, monkeypatch
):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")

    async def _fake_provision_fails(db, verified_name=None, country_code=None):
        raise PoolProvisioningError("Twilio has no numbers left", attempt=None)

    monkeypatch.setattr(pool_provisioning, "provision_pool_number", _fake_provision_fails)

    resp = client.put(
        f"{BASE}/{created['id']}", json={"reassign_from_pool": True}, headers=headers
    )

    assert resp.status_code == 502


def test_update_account_reassign_from_pool_not_configured_returns_503(client, auth_user, monkeypatch):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", None)

    resp = client.put(
        f"{BASE}/{created['id']}", json={"reassign_from_pool": True}, headers=headers
    )

    assert resp.status_code == 503


def test_create_account_auto_assigns_from_pool_when_credentials_omitted(
    client, auth_user, db_session, monkeypatch
):
    _, headers = auth_user
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")
    db_session.add(PhoneNumberPool(phone_number_id="POOL_ACC_1"))
    db_session.commit()

    # No respx route registered at all — a pool assignment must not attempt
    # any Meta validation call, or this test would fail with a connection error.
    resp = client.post(BASE, json={"name": "Auto Shop"}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["phone_number_id"] == "POOL_ACC_1"


def test_create_account_pool_exhausted_returns_409(client, auth_user, monkeypatch):
    _, headers = auth_user
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")

    resp = client.post(BASE, json={"name": "No Pool Shop"}, headers=headers)

    assert resp.status_code == 409


def test_create_account_partial_credentials_returns_400(client, auth_user):
    _, headers = auth_user

    resp = client.post(
        BASE,
        json={"name": "Half Creds Shop", "phone_number_id": "ONLY_PHONE"},
        headers=headers,
    )

    assert resp.status_code == 400


@respx.mock
def test_create_account_duplicate_phone_number_id_returns_clean_400(client, auth_user):
    _, headers = auth_user
    respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
    client.post(BASE, json=_account_payload(), headers=headers)

    resp = client.post(BASE, json=_account_payload(name="Second Shop"), headers=headers)

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_delete_account_releases_pool_number(client, auth_user, db_session, monkeypatch):
    _, headers = auth_user
    monkeypatch.setattr(settings, "WHATSAPP_POOL_ACCESS_TOKEN", "platform-token")
    db_session.add(PhoneNumberPool(phone_number_id="POOL_ACC_2"))
    db_session.commit()
    created = client.post(BASE, json={"name": "Auto Shop 2"}, headers=headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    pool_row = (
        db_session.query(PhoneNumberPool)
        .filter(PhoneNumberPool.phone_number_id == "POOL_ACC_2")
        .first()
    )
    assert pool_row.status.value == "RELEASED"
    assert pool_row.whatsapp_account_id is None


def test_delete_account_not_found_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.delete(f"{BASE}/{created['id']}", headers=other_user_headers)

    assert resp.status_code == 404


def test_delete_account_requires_auth(client):
    resp = client.delete(f"{BASE}/1")

    assert resp.status_code in (401, 403)


def test_get_account_status_returns_meta_fields(client, auth_user):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    with respx.mock:
        respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "verified_name": "My Shop",
                    "display_phone_number": "+1 555-000-0000",
                    "quality_rating": "GREEN",
                    "code_verification_status": "VERIFIED",
                    "status": "CONNECTED",
                    "platform_type": "CLOUD_API",
                },
            )
        )
        resp = client.get(f"{BASE}/{created['id']}/status", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified_name"] == "My Shop"
    assert body["quality_rating"] == "GREEN"
    assert body["status"] == "CONNECTED"


def test_get_account_status_404_for_other_user(client, auth_user, other_user_headers):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    resp = client.get(f"{BASE}/{created['id']}/status", headers=other_user_headers)

    assert resp.status_code == 404


def test_get_account_status_502_when_meta_errors(client, auth_user):
    _, headers = auth_user
    with respx.mock:
        respx.get(url__regex=VALIDATE_URL_RE).mock(return_value=httpx.Response(200))
        created = client.post(BASE, json=_account_payload(), headers=headers).json()

    with respx.mock:
        respx.get(url__regex=r"https://graph\.facebook\.com/.+/PHONE1").mock(
            return_value=httpx.Response(400, json={"error": {}})
        )
        resp = client.get(f"{BASE}/{created['id']}/status", headers=headers)

    assert resp.status_code == 502


def test_get_account_status_requires_auth(client):
    resp = client.get(f"{BASE}/1/status")

    assert resp.status_code in (401, 403)
