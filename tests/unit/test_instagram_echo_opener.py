"""Instagram DM echo handling: hand-sent opener + mid-chat handoff.

Meta echoes the shop side's own DM sends back through the same messaging
webhook (`message.is_echo`). An echo whose mid is already saved is Amara's
own send (ignore); a new mid is something a person typed in the Instagram
inbox — a hand-sent opener when Amara hasn't replied in that conversation
yet (save, no reply), otherwise a mid-chat takeover (save + HUMAN_HANDOFF
until /resume).
"""

import pytest

import app.api.v1.instagram as webhook_module
import app.tasks.instagram_dm_processing as task_module
from app.core.instagram_webhook_parser import InstagramWebhookParser
from app.crud import instagram as crud_instagram
from app.db.models import (
    ConversationState,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    User,
    WhatsAppAccount,
)
from app.services.merchant_handler import handle_merchant_message

WEBHOOK_URL = "/api/v1/instagram/webhook"


def _echo_payload(ig_business_id: str, recipient_igsid: str, mid: str, text: str) -> dict:
    """An echo event: sender is the connected account itself, the customer
    is the recipient — the mirror image of an inbound DM event."""
    return {
        "entry": [
            {
                "id": ig_business_id,
                "messaging": [
                    {
                        "sender": {"id": ig_business_id},
                        "recipient": {"id": recipient_igsid},
                        "message": {"mid": mid, "text": text, "is_echo": True},
                    }
                ],
            }
        ]
    }


@pytest.fixture(autouse=True)
def _no_eager_token_refresh(monkeypatch):
    """Fixture accounts have no token_refreshed_at (stale by definition),
    so without this every webhook post would run the refresh task inline
    in eager mode and attempt real HTTP."""
    import app.tasks.instagram_token_refresh as refresh_module

    monkeypatch.setattr(
        refresh_module.refresh_account_token, "apply_async",
        lambda args=None, **kw: None,
    )


@pytest.fixture(autouse=True)
def _no_display_name_lookup(monkeypatch):
    """Echoes for brand-new customers would otherwise trigger a real Graph
    API profile lookup for the username backfill."""

    async def _none(account, igsid):
        return None

    monkeypatch.setattr(webhook_module, "_fetch_instagram_display_name", _none)


@pytest.fixture(autouse=True)
def _task_uses_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(task_module, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000910")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_echo",
        access_token="ig-tok",
        username="echoshop",
        ai_enabled=True,
        user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session, account):
    c = InstagramCustomer(
        ig_scoped_id="igsid_echo_1", username="echo_shopper", account_id=account.id
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def dispatch_recorder(monkeypatch):
    """Records webhook->task dispatches instead of running them."""
    calls = []

    def fake_apply_async_or_rollback(task, args, db, model, row_id):
        calls.append((task, args))

    monkeypatch.setattr(
        webhook_module, "apply_async_or_rollback", fake_apply_async_or_rollback
    )
    return calls


class _FakeProvider:
    def __init__(self, access_token, response=None):
        self.access_token = access_token
        self.response = response or {"message_id": "mid_out_1"}
        self.calls = []

    async def send_direct_message(self, ig_business_id, recipient_id, message):
        self.calls.append((ig_business_id, recipient_id, message))
        return self.response


@pytest.fixture
def fake_llm_reply(monkeypatch):
    async def fake_generate_dm_response(**kwargs):
        fake_generate_dm_response.called_with = {
            "message_body": kwargs["message_body"],
            "conversation_history": kwargs["conversation_history"],
        }
        return "It's 45000 TZS."

    fake_generate_dm_response.called_with = None

    class FakeLLMService:
        generate_dm_response = staticmethod(fake_generate_dm_response)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    return fake_generate_dm_response


# --- parser: parse_outbound_echoes -------------------------------------------


def test_parser_surfaces_echo_with_customer_resolved_from_recipient():
    data = _echo_payload("ig_biz_echo", "igsid_echo_1", "mid_echo_1", "Karibu! Mzigo umefika.")

    parsed = InstagramWebhookParser.parse_outbound_echoes(data)

    assert parsed == [
        {
            "ig_business_id": "ig_biz_echo",
            "sender_id": "igsid_echo_1",
            "ig_message_id": "mid_echo_1",
            "body": "Karibu! Mzigo umefika.",
            "is_echo": True,
        }
    ]


def test_parser_echo_ignores_inbound_and_non_message_events():
    data = {
        "entry": [
            {
                "id": "ig_biz_echo",
                "messaging": [
                    {
                        "sender": {"id": "igsid_echo_1"},
                        "recipient": {"id": "ig_biz_echo"},
                        "message": {"mid": "mid_in_1", "text": "hi"},
                    },
                    {
                        "sender": {"id": "igsid_echo_1"},
                        "recipient": {"id": "ig_biz_echo"},
                        "read": {"mid": "mid_in_1"},
                    },
                ],
            }
        ]
    }

    assert InstagramWebhookParser.parse_outbound_echoes(data) == []


# --- case 1: echo of a message Amara sent -------------------------------------


def test_echo_of_amara_sent_message_is_ignored(
    client, db_session, account, customer, dispatch_recorder
):
    """Amara's own sends were already saved at send time under the same
    mid, so their echo is a no-op — as when the parser dropped every echo."""
    crud_instagram.create_direct_message(
        db_session,
        account_id=account.id,
        customer_id=customer.id,
        direction="outbound",
        body="It's 45000 TZS.",
        ig_message_id="mid_amara_1",
    )

    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_echo_1", "mid_amara_1", "It's 45000 TZS."),
    )

    assert resp.status_code == 200
    assert db_session.query(InstagramDirectMessage).count() == 1
    assert db_session.query(InstagramConversationContext).count() == 0
    assert dispatch_recorder == []


# --- case 2: hand-sent opener --------------------------------------------------


def test_hand_sent_opener_is_saved_without_reply(
    client, db_session, account, dispatch_recorder
):
    """No Amara reply in this conversation yet: the person-typed echo is
    the merchant's hand-sent opener — saved as shop-side history, no reply
    dispatched, no handoff."""
    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_new_1", "mid_opener_1", "Mambo! Nikuwekee ngapi?"),
    )

    assert resp.status_code == 200

    rows = db_session.query(InstagramDirectMessage).all()
    assert len(rows) == 1
    assert rows[0].direction == "outbound"
    assert rows[0].body == "Mambo! Nikuwekee ngapi?"
    assert rows[0].ig_message_id == "mid_opener_1"

    saved_customer = (
        db_session.query(InstagramCustomer)
        .filter(InstagramCustomer.ig_scoped_id == "igsid_new_1")
        .one()
    )
    assert rows[0].customer_id == saved_customer.id

    assert db_session.query(InstagramConversationContext).count() == 0
    assert dispatch_recorder == []


def test_opener_dispatches_no_reply_task(client, db_session, account, dispatch_recorder):
    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_new_2", "mid_opener_2", "Karibu dukani!"),
    )

    assert resp.status_code == 200
    assert dispatch_recorder == []
    assert db_session.query(InstagramDirectMessage).count() == 1


def test_opener_appears_in_llm_history_on_customer_reply(
    client, db_session, account, fake_llm_reply, monkeypatch
):
    """When the customer answers the opener, process_inbound_dm hands the
    LLM the opener as the shop side's first turn (direction outbound —
    rendered as Amara's line by generate_dm_response)."""
    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_new_3", "mid_opener_3", "Nina offer leo!"),
    )
    assert resp.status_code == 200
    saved_customer = (
        db_session.query(InstagramCustomer)
        .filter(InstagramCustomer.ig_scoped_id == "igsid_new_3")
        .one()
    )

    reply = crud_instagram.create_direct_message(
        db_session,
        account_id=account.id,
        customer_id=saved_customer.id,
        direction="inbound",
        body="Nipe bei ya jumla",
        ig_message_id="mid_in_reply_1",
    )

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    # Captured before the call — the task's `finally: db.close()` closes
    # this shared test session, which would otherwise expire these objects.
    account_id, customer_id, dm_id = account.id, saved_customer.id, reply.id
    result = task_module.process_inbound_dm(account_id, customer_id, dm_id)

    assert result == "Processed."
    assert fake_llm_reply.called_with["message_body"] == "Nipe bei ya jumla"
    assert fake_llm_reply.called_with["conversation_history"] == [
        {"direction": "outbound", "body": "Nina offer leo!"}
    ]


def test_echo_redelivery_is_saved_once(client, db_session, account, dispatch_recorder):
    """Same redelivery dedupe as inbound DMs: the second delivery hits the
    already-saved mid and no-ops."""
    payload = _echo_payload("ig_biz_echo", "igsid_new_4", "mid_opener_4", "Habari!")

    assert client.post(WEBHOOK_URL, json=payload).status_code == 200
    assert client.post(WEBHOOK_URL, json=payload).status_code == 200

    rows = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.ig_message_id == "mid_opener_4")
        .all()
    )
    assert len(rows) == 1
    assert dispatch_recorder == []


# --- case 3: person-typed echo after Amara replied ------------------------------


def _seed_ongoing_chat(db_session, account, customer):
    crud_instagram.create_direct_message(
        db_session,
        account_id=account.id,
        customer_id=customer.id,
        direction="inbound",
        body="Bei gani?",
        ig_message_id="mid_in_1",
    )
    crud_instagram.create_direct_message(
        db_session,
        account_id=account.id,
        customer_id=customer.id,
        direction="outbound",
        body="It's 45000 TZS.",
        ig_message_id="mid_out_1",
    )


def test_mid_chat_echo_sets_handoff_and_silences_amara(
    client, db_session, account, customer, fake_llm_reply, monkeypatch
):
    """Amara already replied in this thread, so a person-typed echo means
    the merchant is taking over: saved, and the conversation goes
    HUMAN_HANDOFF so Amara stays silent on the customer's next message."""
    _seed_ongoing_chat(db_session, account, customer)

    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_echo_1", "mid_takeover_1", "Mimi muhusika — ngoja kidogo"),
    )

    assert resp.status_code == 200

    takeover = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.ig_message_id == "mid_takeover_1")
        .one()
    )
    assert takeover.direction == "outbound"
    assert takeover.body == "Mimi muhusika — ngoja kidogo"

    context = (
        db_session.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer.id)
        .one()
    )
    assert context.state == ConversationState.HUMAN_HANDOFF
    assert context.handoff_at is not None

    # The customer's next message gets no reply while the handoff stands.
    next_inbound = crud_instagram.create_direct_message(
        db_session,
        account_id=account.id,
        customer_id=customer.id,
        direction="inbound",
        body="Sawa, nasubiri",
        ig_message_id="mid_in_2",
    )
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    account_id, customer_id, dm_id = account.id, customer.id, next_inbound.id
    result = task_module.process_inbound_dm(account_id, customer_id, dm_id)

    assert result == "Conversation is in human handoff; not replying."
    assert fake_provider.calls == []
    assert fake_llm_reply.called_with is None


async def test_resume_reactivates_after_echo_handoff(client, db_session, owner, account, customer):
    """/resume @username (and the crud call behind it) flips the echo-set
    handoff back to NEGOTIATING."""
    wa_account = WhatsAppAccount(
        name="Merchant WA", phone_number_id="PHONE_ECHO", access_token="tok", user_id=owner.id
    )
    db_session.add(wa_account)
    db_session.commit()

    _seed_ongoing_chat(db_session, account, customer)
    resp = client.post(
        WEBHOOK_URL,
        json=_echo_payload("ig_biz_echo", "igsid_echo_1", "mid_takeover_2", "Nipo hapa"),
    )
    assert resp.status_code == 200
    context = (
        db_session.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer.id)
        .one()
    )
    assert context.state == ConversationState.HUMAN_HANDOFF

    reply = await handle_merchant_message(db_session, wa_account, "/resume @echo_shopper", owner.username)

    assert "resumed" in reply
    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert context.handoff_at is None

    # The crud layer behind the dashboard resume endpoint behaves the same.
    crud_instagram.set_human_handoff(db_session, customer.id)
    crud_instagram.resume_conversation(db_session, customer.id)
    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
