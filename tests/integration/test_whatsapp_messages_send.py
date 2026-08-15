import httpx
import pytest
import respx

from app.core.security import create_access_token
from app.db.models import Message, User, WhatsAppAccount

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


@pytest.fixture
def owner_and_headers(db_session):
    owner = User(username="255700000600")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner, {"Authorization": f"Bearer {create_access_token(owner.id)}"}


@pytest.fixture
def account(db_session, owner_and_headers):
    owner, _ = owner_and_headers
    account = WhatsAppAccount(
        name="Send Shop", phone_number_id="SEND_PHONE", access_token="tok", user_id=owner.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@respx.mock
def test_send_text_message(client, db_session, owner_and_headers, account):
    _, headers = owner_and_headers
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.send1"}]})
    )

    resp = client.post(
        "/api/v1/whatsapp/messages/send",
        headers={**headers, "X-WhatsApp-Account-ID": str(account.id)},
        json={"to": "255711119999", "type": "text", "text_body": "Hello there"},
    )

    assert resp.status_code == 200
    logged = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert logged is not None
    assert logged.body == "Hello there"
    assert logged.whatsapp_message_id == "wamid.send1"


@respx.mock
def test_send_template_message(client, db_session, owner_and_headers, account):
    _, headers = owner_and_headers
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.send2"}]})
    )

    resp = client.post(
        "/api/v1/whatsapp/messages/send",
        headers={**headers, "X-WhatsApp-Account-ID": str(account.id)},
        json={"to": "255711119999", "type": "template", "template_name": "hello_world"},
    )

    assert resp.status_code == 200
    assert route.called
    logged = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert logged.type == "template"
    assert logged.body == "hello_world"


@respx.mock
def test_send_media_message(client, db_session, owner_and_headers, account):
    _, headers = owner_and_headers
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.send3"}]})
    )

    resp = client.post(
        "/api/v1/whatsapp/messages/send",
        headers={**headers, "X-WhatsApp-Account-ID": str(account.id)},
        json={"to": "255711119999", "type": "image", "media_id": "MEDIA123"},
    )

    assert resp.status_code == 200
    assert route.called
    logged = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert logged.type == "image"
    assert logged.body == "MEDIA123"
