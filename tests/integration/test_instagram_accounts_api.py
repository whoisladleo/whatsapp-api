import httpx
import respx

from app.db.models import InstagramAccount, User

URL = "/api/v1/instagram/accounts"
SUBSCRIBE_URL_RE = r"https://graph\.instagram\.com/me/subscribed_apps"


def test_list_accounts_requires_auth(client):
    resp = client.get(URL)
    assert resp.status_code in (401, 403)


def test_list_accounts_empty_when_none_connected(client, auth_user):
    _, headers = auth_user
    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_accounts_returns_only_current_users_accounts(client, db_session, auth_user):
    user, headers = auth_user
    other = User(username="255700000002")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    mine = InstagramAccount(
        ig_business_id="ig_mine", access_token="tok1", username="my_shop", user_id=user.id
    )
    theirs = InstagramAccount(
        ig_business_id="ig_theirs", access_token="tok2", username="their_shop", user_id=other.id
    )
    db_session.add_all([mine, theirs])
    db_session.commit()

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["ig_business_id"] == "ig_mine"
    assert body[0]["username"] == "my_shop"


def test_list_accounts_excludes_disconnected_accounts(client, db_session, auth_user):
    user, headers = auth_user
    db_session.add(
        InstagramAccount(ig_business_id="ig_gone", access_token="", username="old_shop", user_id=user.id)
    )
    db_session.commit()

    resp = client.get(URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_disconnect_requires_auth(client, db_session):
    owner = User(username="255700000003")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    account = InstagramAccount(ig_business_id="ig_x", access_token="tok", user_id=owner.id)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    resp = client.delete(f"{URL}/{account.id}")

    assert resp.status_code in (401, 403)


@respx.mock
def test_disconnect_clears_token_and_removes_from_list(client, db_session, auth_user):
    user, headers = auth_user
    account = InstagramAccount(
        ig_business_id="ig_mine2", access_token="tok1", username="my_shop2", user_id=user.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    route = respx.delete(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    resp = client.delete(f"{URL}/{account.id}", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "disconnected"}
    assert route.called

    db_session.refresh(account)
    assert account.access_token == ""
    assert account.ai_enabled is False

    list_resp = client.get(URL, headers=headers)
    assert list_resp.json() == []


@respx.mock
def test_disconnect_still_succeeds_when_meta_unsubscribe_fails(client, db_session, auth_user):
    """Meta-side failure is best-effort — the merchant's local disconnect
    must still go through so Amara stops acting on this account either way."""
    user, headers = auth_user
    account = InstagramAccount(
        ig_business_id="ig_mine3", access_token="tok1", username="my_shop3", user_id=user.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    respx.delete(url__regex=SUBSCRIBE_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )

    resp = client.delete(f"{URL}/{account.id}", headers=headers)

    assert resp.status_code == 200
    db_session.refresh(account)
    assert account.access_token == ""


def test_disconnect_404_for_unknown_account(client, auth_user):
    _, headers = auth_user

    resp = client.delete(f"{URL}/999999", headers=headers)

    assert resp.status_code == 404


def test_disconnect_404_for_another_users_account(client, db_session, auth_user):
    _, headers = auth_user
    other = User(username="255700000004")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    theirs = InstagramAccount(ig_business_id="ig_theirs2", access_token="tok", user_id=other.id)
    db_session.add(theirs)
    db_session.commit()
    db_session.refresh(theirs)

    resp = client.delete(f"{URL}/{theirs.id}", headers=headers)

    assert resp.status_code == 404
