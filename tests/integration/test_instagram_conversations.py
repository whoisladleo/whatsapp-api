import uuid

import pytest

from app.core.instagram_provider import InstagramProvider
from app.db.models import (
    ConversationState,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    User,
)

CONVERSATIONS_URL = "/api/v1/instagram/conversations"
HANDOFFS_URL = "/api/v1/instagram/handoffs"
MESSAGES_URL = "/api/v1/instagram/messages"
SEND_URL = "/api/v1/instagram/messages/send"


@pytest.fixture
def account(db_session, auth_user):
    user, _ = auth_user
    acct = InstagramAccount(
        ig_business_id="ig_conv_1", access_token="ig-tok", username="my_shop", user_id=user.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session, account):
    c = InstagramCustomer(ig_scoped_id="igsid_1", username="commenter1", account_id=account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


# --- GET /conversations --------------------------------------------------------


def test_conversations_requires_auth(client):
    resp = client.get(CONVERSATIONS_URL)
    assert resp.status_code in (401, 403)


def test_conversations_404_without_connected_account(client, auth_user):
    _, headers = auth_user
    resp = client.get(CONVERSATIONS_URL, headers=headers)
    assert resp.status_code == 404


def test_conversations_lists_customers_with_messages(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    db_session.add(
        InstagramDirectMessage(
            account_id=account.id, customer_id=customer.id, direction="inbound", body="hi"
        )
    )
    db_session.commit()

    resp = client.get(CONVERSATIONS_URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["customer_id"] == str(customer.id)
    assert body[0]["last_message_body"] == "hi"


def test_conversations_excludes_other_users_accounts(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    other = User(username="255700000777")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    other_account = InstagramAccount(
        ig_business_id="ig_other_conv", access_token="tok", username="other_shop", user_id=other.id,
    )
    db_session.add(other_account)
    db_session.commit()
    db_session.refresh(other_account)
    other_customer = InstagramCustomer(
        ig_scoped_id="igsid_other", account_id=other_account.id
    )
    db_session.add(other_customer)
    db_session.commit()
    db_session.refresh(other_customer)
    db_session.add(
        InstagramDirectMessage(
            account_id=other_account.id, customer_id=other_customer.id, direction="inbound", body="hi"
        )
    )
    db_session.commit()

    resp = client.get(CONVERSATIONS_URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


# --- GET /messages ---------------------------------------------------------------


def test_messages_requires_auth(client, account, customer):
    resp = client.get(MESSAGES_URL, params={"customer_id": customer.id})
    assert resp.status_code in (401, 403)


def test_messages_returns_history_oldest_first(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    db_session.add_all([
        InstagramDirectMessage(account_id=account.id, customer_id=customer.id, direction="inbound", body="first"),
        InstagramDirectMessage(account_id=account.id, customer_id=customer.id, direction="outbound", body="second"),
    ])
    db_session.commit()

    resp = client.get(MESSAGES_URL, params={"customer_id": customer.id}, headers=headers)

    assert resp.status_code == 200
    bodies = [m["body"] for m in resp.json()]
    assert bodies == ["first", "second"]


def test_messages_404_for_customer_belonging_to_another_account(client, db_session, auth_user, account):
    _, headers = auth_user
    other = User(username="255700000778")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    other_account = InstagramAccount(
        ig_business_id="ig_other_msg", access_token="tok", user_id=other.id,
    )
    db_session.add(other_account)
    db_session.commit()
    db_session.refresh(other_account)
    other_customer = InstagramCustomer(ig_scoped_id="igsid_other2", account_id=other_account.id)
    db_session.add(other_customer)
    db_session.commit()
    db_session.refresh(other_customer)

    resp = client.get(MESSAGES_URL, params={"customer_id": other_customer.id}, headers=headers)

    assert resp.status_code == 404


# --- POST /messages/send ----------------------------------------------------------


def test_send_requires_auth(client, customer):
    resp = client.post(SEND_URL, json={"customer_id": str(customer.id), "message": "hi"})
    assert resp.status_code in (401, 403)


def test_send_success_creates_outbound_message(client, db_session, auth_user, account, customer, monkeypatch):
    _, headers = auth_user

    async def fake_send_direct_message(self, ig_business_id, recipient_id, message, tag=None):
        assert ig_business_id == "ig_conv_1"
        assert recipient_id == "igsid_1"
        assert message == "We'll follow up shortly!"
        # A manual dashboard reply is a genuine human agent response — see #13.
        assert tag == "HUMAN_AGENT"
        return {"message_id": "mid_sent_1"}

    monkeypatch.setattr(InstagramProvider, "send_direct_message", fake_send_direct_message)

    resp = client.post(
        SEND_URL,
        json={"customer_id": str(customer.id), "message": "We'll follow up shortly!"},
        headers=headers,
    )

    assert resp.status_code == 200
    outbound = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .first()
    )
    assert outbound is not None
    assert outbound.body == "We'll follow up shortly!"
    assert outbound.ig_message_id == "mid_sent_1"


def test_send_failure_returns_422(client, auth_user, account, customer, monkeypatch):
    _, headers = auth_user

    async def fake_send_direct_message(self, ig_business_id, recipient_id, message, tag=None):
        return {"error": {"message": "rate limited"}}

    monkeypatch.setattr(InstagramProvider, "send_direct_message", fake_send_direct_message)

    resp = client.post(
        SEND_URL, json={"customer_id": str(customer.id), "message": "hi"}, headers=headers
    )

    # 422, not 502: this is Meta rejecting a well-formed request, not a
    # gateway failure — 502 was found live to get its JSON body swallowed
    # and replaced by the reverse proxy in front of this API.
    assert resp.status_code == 422
    # Regression: used to always return a generic message, discarding
    # Meta's actual reason (e.g. outside the messaging window, invalid
    # token) — the dashboard renders this detail directly to the merchant.
    assert resp.json()["detail"] == "rate limited"


def test_send_failure_falls_back_to_generic_message_without_meta_detail(
    client, auth_user, account, customer, monkeypatch
):
    _, headers = auth_user

    async def fake_send_direct_message(self, ig_business_id, recipient_id, message, tag=None):
        return {"error": {}}

    monkeypatch.setattr(InstagramProvider, "send_direct_message", fake_send_direct_message)

    resp = client.post(
        SEND_URL, json={"customer_id": str(customer.id), "message": "hi"}, headers=headers
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Failed to send Instagram message"


def test_send_404_for_unknown_customer(client, auth_user, account):
    _, headers = auth_user

    resp = client.post(SEND_URL, json={"customer_id": str(uuid.uuid4()), "message": "hi"}, headers=headers)

    assert resp.status_code == 404


# --- GET /handoffs + POST /handoffs/{id}/resume -------------------------------


def test_handoffs_requires_auth(client):
    resp = client.get(HANDOFFS_URL)
    assert resp.status_code in (401, 403)


def test_handoffs_lists_only_human_handoff_state(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    db_session.add(
        InstagramDirectMessage(account_id=account.id, customer_id=customer.id, direction="inbound", body="help!")
    )
    db_session.add(
        InstagramConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    )
    db_session.commit()

    resp = client.get(HANDOFFS_URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["customer_id"] == str(customer.id)
    assert body[0]["last_message_body"] == "help!"


def test_resume_success(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    db_session.add(
        InstagramConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    )
    db_session.commit()

    resp = client.post(f"{HANDOFFS_URL}/{customer.id}/resume", headers=headers)

    assert resp.status_code == 200
    context = (
        db_session.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer.id)
        .first()
    )
    assert context.state == ConversationState.NEGOTIATING


def test_resume_404_for_unknown_customer(client, auth_user, account):
    _, headers = auth_user

    resp = client.post(f"{HANDOFFS_URL}/{uuid.uuid4()}/resume", headers=headers)

    assert resp.status_code == 404


def test_resume_400_when_not_in_handoff(client, db_session, auth_user, account, customer):
    _, headers = auth_user
    db_session.add(InstagramConversationContext(customer_id=customer.id, state=ConversationState.NEGOTIATING))
    db_session.commit()

    resp = client.post(f"{HANDOFFS_URL}/{customer.id}/resume", headers=headers)

    assert resp.status_code == 400
