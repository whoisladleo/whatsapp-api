from app.db.models import InstagramAccount, User

URL = "/api/v1/instagram/accounts"


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
