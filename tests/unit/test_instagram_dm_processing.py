import pytest

import app.tasks.instagram_dm_processing as task_module
from app.db.models import (
    ConversationState,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    User,
)


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(task_module, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000900")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_1",
        access_token="ig-tok",
        username="shopowner",
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
        ig_scoped_id="igsid_1", username="commenter1", account_id=account.id
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def inbound_dm(db_session, account, customer):
    dm = InstagramDirectMessage(
        account_id=account.id,
        customer_id=customer.id,
        direction="inbound",
        body="how much is it?",
        ig_message_id="mid_1",
    )
    db_session.add(dm)
    db_session.commit()
    db_session.refresh(dm)
    return dm


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
        # Captured as plain values rather than the ORM objects themselves —
        # the task's `finally: db.close()` detaches them before this
        # fixture's caller gets to make assertions.
        fake_generate_dm_response.called_with = {
            "message_body": kwargs["message_body"],
            "account_id": kwargs["account"].id,
            "account_user_id": kwargs["account"].user_id,
            "context_customer_id": kwargs["context"].customer_id,
            "account_name": kwargs["account_name"],
            "conversation_history": kwargs["conversation_history"],
        }
        return "It's 45000 TZS."

    fake_generate_dm_response.called_with = None

    class FakeLLMService:
        generate_dm_response = staticmethod(fake_generate_dm_response)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    return fake_generate_dm_response


def test_missing_account_or_customer_is_a_no_op(db_session):
    result = task_module.process_inbound_dm(999999, 999999, 999999)
    assert result == "Account or customer not found."


def test_llm_unavailable_is_a_no_op(db_session, account, customer, inbound_dm, monkeypatch):
    monkeypatch.setattr(task_module, "llm_service", None)

    result = task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert result == "LLM unavailable."


def test_sends_dm_reply_on_success(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    account_id, customer_id, dm_id, merchant_id = (
        account.id,
        customer.id,
        inbound_dm.id,
        account.user_id,
    )
    result = task_module.process_inbound_dm(account_id, customer_id, dm_id)

    assert result == "Processed."
    assert fake_provider.calls == [("ig_biz_1", "igsid_1", "It's 45000 TZS.")]

    outbound = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .first()
    )
    assert outbound is not None
    assert outbound.body == "It's 45000 TZS."
    assert outbound.ig_message_id == "mid_out_1"

    assert fake_llm_reply.called_with["message_body"] == "how much is it?"
    assert fake_llm_reply.called_with["account_id"] == account_id
    assert fake_llm_reply.called_with["account_user_id"] == merchant_id
    assert fake_llm_reply.called_with["context_customer_id"] == customer_id
    assert fake_llm_reply.called_with["account_name"] == "shopowner"
    assert fake_llm_reply.called_with["conversation_history"] == []


def test_no_reply_generated_sends_nothing(db_session, account, customer, inbound_dm, monkeypatch):
    async def fake_generate_dm_response(**kwargs):
        return None

    class FakeLLMService:
        generate_dm_response = staticmethod(fake_generate_dm_response)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert result == "No reply generated."
    assert fake_provider.calls == []


def test_existing_handoff_conversation_is_not_replied_to(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    context = InstagramConversationContext(
        customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF
    )
    db_session.add(context)
    db_session.commit()

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert result == "Conversation is in human handoff; not replying."
    assert fake_provider.calls == []


def test_reply_reflects_whatever_state_generate_dm_response_left_the_context_in(
    db_session, account, customer, inbound_dm, monkeypatch
):
    """
    generate_dm_response owns handoff-state mutation directly on the
    InstagramConversationContext it's given (see tests/unit/test_llm_dm_response.py
    for that behavior) — the task just needs to pass the same context object
    through and not clobber whatever state it ends up in.
    """
    async def fake_generate_dm_response(**kwargs):
        kwargs["context"].state = ConversationState.HUMAN_HANDOFF
        db_session.commit()
        return "Someone from the team will follow up shortly."

    class FakeLLMService:
        generate_dm_response = staticmethod(fake_generate_dm_response)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    account_id, customer_id, dm_id = account.id, customer.id, inbound_dm.id
    task_module.process_inbound_dm(account_id, customer_id, dm_id)

    context = (
        db_session.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer_id)
        .first()
    )
    assert context.state == ConversationState.HUMAN_HANDOFF


def test_send_failure_does_not_create_outbound_row(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider("ig-tok", response={"error": {"message": "rate limited"}})
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert result == "Send failed."
    outbound = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .first()
    )
    assert outbound is None
