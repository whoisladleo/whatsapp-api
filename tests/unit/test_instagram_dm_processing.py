from datetime import datetime

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


def test_second_dispatch_for_same_inbound_message_is_a_no_op(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    """
    Regression test: a single inbound message used to be processed twice
    end-to-end (two LLM calls, two sends, two orders for the same deal) if
    process_inbound_dm got dispatched more than once for the same dm_row_id
    — whether from webhook redelivery racing past handle_webhook's own
    dedup check, or Celery's own at-least-once task redelivery. See #33.
    """
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    # Captured before either call — the task's own `finally: db.close()`
    # closes this shared test session after the first call (see
    # _use_test_session), which would otherwise expire these ORM objects.
    account_id, customer_id, dm_id = account.id, customer.id, inbound_dm.id

    first = task_module.process_inbound_dm(account_id, customer_id, dm_id)
    second = task_module.process_inbound_dm(account_id, customer_id, dm_id)

    assert first == "Processed."
    assert second == "Already processed; skipping duplicate dispatch."
    assert len(fake_provider.calls) == 1

    outbound_count = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.direction == "outbound")
        .count()
    )
    assert outbound_count == 1


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


# --- Claude-outage retries (#38) ----------------------------------------------

def test_llm_outage_retry_reclaims_and_replies_once(db_session, account, customer, inbound_dm, monkeypatch):
    calls = []

    async def flaky(**kwargs):
        calls.append(kwargs["retry_on_outage"])
        if len(calls) == 1:
            raise task_module.LLMUnavailable("overloaded")
        return "It's 45000 TZS."

    class FakeLLMService:
        generate_dm_response = staticmethod(flaky)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    task_module.process_inbound_dm.apply_async(args=[account.id, customer.id, inbound_dm.id])

    assert calls == [True, True]
    assert len(fake_provider.calls) == 1


def test_outage_retry_claim_is_compare_and_swap(db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch):
    """
    While a retry is pending, the row stays claimed: a webhook redelivery
    (claimed_at=None) and a stale retry (wrong claimed_at) are both refused;
    only the retry holding the exact claim gets through.
    """
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: _FakeProvider("ig-tok"))
    claimed_at = datetime(2026, 10, 23, 12, 0, 0, 123456)
    inbound_dm.processed_at = claimed_at
    db_session.commit()
    ids = (account.id, customer.id, inbound_dm.id)

    assert task_module.process_inbound_dm(*ids) == "Already processed; skipping duplicate dispatch."
    assert (
        task_module.process_inbound_dm(*ids, 1, datetime(2026, 1, 1).isoformat())
        == "Already processed; skipping duplicate dispatch."
    )
    assert task_module.process_inbound_dm(*ids, 1, claimed_at.isoformat()) == "Processed."


def test_resume_turn_skips_claim_on_already_processed_row(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    """Take-over turns re-run an already-claimed row (#149): resume=True
    skips the claim CAS instead of returning 'Already processed'."""
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)
    ids = (account.id, customer.id, inbound_dm.id)

    assert task_module.process_inbound_dm(*ids) == "Processed."
    assert task_module.process_inbound_dm(*ids) == "Already processed; skipping duplicate dispatch."
    fake_provider.response = {"message_id": "mid_out_2"}
    assert task_module.process_inbound_dm(*ids, 0, None, 0, True) == "Processed."
    assert fake_provider.calls == [("ig_biz_1", "igsid_1", "It's 45000 TZS.")] * 2


def test_resume_outage_retry_propagates_resume_flag(
    db_session, account, customer, inbound_dm, monkeypatch
):
    """A resume turn that hits an LLM outage retries with resume intact —
    without propagation the retry would hit the already-claimed row and
    drop the take-over reply as a duplicate (#149)."""
    calls = []

    async def flaky(**kwargs):
        calls.append(kwargs["retry_on_outage"])
        if len(calls) == 1:
            raise task_module.LLMUnavailable("overloaded")
        return "It's 45000 TZS."

    class FakeLLMService:
        generate_dm_response = staticmethod(flaky)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)
    inbound_dm.processed_at = datetime(2026, 10, 23, 12, 0, 0)
    db_session.commit()

    task_module.process_inbound_dm.apply_async(
        args=[account.id, customer.id, inbound_dm.id, 0, None, 0, True]
    )

    assert calls == [True, True]
    assert len(fake_provider.calls) == 1


def test_send_failure_alerts_merchant(db_session, account, customer, inbound_dm, monkeypatch):
    monkeypatch.setattr(task_module, "_send_with_retries", _no_retry_send)
    alerts = []

    async def reply(**kwargs):
        return "It's 45000 TZS."

    async def record_alert(db, ig_account, ig_customer, customer_message):
        alerts.append(customer_message)

    class FakeLLMService:
        generate_dm_response = staticmethod(reply)
        _notify_merchant_of_instagram_handoff = staticmethod(record_alert)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    monkeypatch.setattr(
        task_module, "InstagramProvider",
        lambda access_token: _FakeProvider("ig-tok", response={"error": {"message": "outside window"}}),
    )

    assert task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id) == "Send failed."
    assert alerts == ["how much is it?"]


async def _no_retry_send(send):
    return await send()


def test_delivered_dm_reply_is_metered(db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch):
    from app.db.models import AmaraEvent, AmaraEventKind

    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: _FakeProvider("ig-tok"))
    merchant_id, account_id = account.user_id, account.id

    task_module.process_inbound_dm(account_id, customer.id, inbound_dm.id)

    replies = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).all()
    assert [(r.merchant_id, r.channel, r.instagram_account_id) for r in replies] == [
        (merchant_id, "instagram_dm", account_id)
    ]


def test_undelivered_dm_reply_is_not_metered(db_session, account, customer, inbound_dm, monkeypatch):
    from app.db.models import AmaraEvent

    monkeypatch.setattr(task_module, "_send_with_retries", _no_retry_send)

    async def reply(**kwargs):
        return "It's 45000 TZS."

    async def no_alert(*a, **kw):
        return None

    class FakeLLMService:
        generate_dm_response = staticmethod(reply)
        _notify_merchant_of_instagram_handoff = staticmethod(no_alert)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    monkeypatch.setattr(
        task_module, "InstagramProvider",
        lambda access_token: _FakeProvider("ig-tok", response={"error": {"message": "outside window"}}),
    )

    task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert db_session.query(AmaraEvent).count() == 0


def test_attachment_only_dm_reaches_llm_as_described_event(
    db_session, account, customer, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)
    dm = InstagramDirectMessage(
        account_id=account.id,
        customer_id=customer.id,
        direction="inbound",
        body="",
        ig_message_id="mid_share1",
        attachments=[{"type": "share", "url": "https://ig.example/p/1"}],
    )
    db_session.add(dm)
    db_session.commit()
    db_session.refresh(dm)

    result = task_module.process_inbound_dm(account.id, customer.id, dm.id)

    assert result == "Processed."
    assert (
        fake_llm_reply.called_with["message_body"]
        == "shared a post: https://ig.example/p/1"
    )


def test_quota_exhausted_sends_holding_without_llm(
    db_session, account, customer, inbound_dm, fake_llm_reply, monkeypatch
):
    """Same meter as WhatsApp: holding DM, no model call, nothing metered (#90)."""
    from datetime import datetime, timedelta

    import app.tasks.usage_alerts as usage_alerts
    from app.db.models import (
        AmaraEvent, AmaraEventKind, Customer, Order, OrderStatus, PaymentType,
        Product, ProductSubscription, ProductSubscriptionStatus,
    )
    from app.services import usage

    owner = db_session.query(User).filter(User.id == account.user_id).one()
    seller = User(username="255700000901")
    db_session.add(seller)
    db_session.commit()
    plan = Product(
        slug="plan-quota", name="Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=seller.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2,
    )
    buyer = Customer(phone_number=owner.username)
    db_session.add_all([plan, buyer])
    db_session.commit()
    order = Order(
        customer_id=buyer.id, product_id=plan.id, quantity=1, agreed_price=100000,
        payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.add(ProductSubscription(
        order_id=order.id, customer_id=buyer.id, product_id=plan.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        next_billing_at=datetime.utcnow() + timedelta(days=5),
    ))
    db_session.commit()
    monkeypatch.setattr(usage_alerts.send_usage_warning, "apply_async", lambda args, **kw: None)
    usage.record_reply(db_session, owner.id, "instagram_dm")
    usage.record_reply(db_session, owner.id, "instagram_dm")

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_dm(account.id, customer.id, inbound_dm.id)

    assert result == "Processed."
    assert fake_provider.calls == [("ig_biz_1", "igsid_1", usage.QUOTA_EXHAUSTED_HOLDING_MESSAGE)]
    assert fake_llm_reply.called_with is None
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).count() == 2
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.LIMIT_BLOCKED).count() == 1


# --- Per-customer lock --------------------------------------------------------
#
# Same acquire-or-redispatch shape as process_inbound_message: without it,
# two same-content deliveries race and the buyer gets contradictory replies
# (a reclose "already on its way" plus a dispatch-failure note, #142).
# Under eager mode the nested apply_async calls run to completion
# synchronously, so the eventual success is observable via side effects,
# not the top-level return value (see the WA lock tests for the full
# reasoning).

def test_lock_contention_retries_then_succeeds(monkeypatch, db_session, account, customer, inbound_dm):
    calls = {"n": 0}
    processed = []
    real_acquire = task_module._acquire_lock

    def flaky_acquire(key):
        calls["n"] += 1
        if calls["n"] < 3:
            return False
        return real_acquire(key)

    monkeypatch.setattr(task_module, "_acquire_lock", flaky_acquire)

    async def fake_process(*args, **kwargs):
        processed.append(kwargs.get("dm_row_id", args[3] if len(args) > 3 else None))
        return "Success"

    monkeypatch.setattr(task_module, "_process", fake_process)

    result = task_module.process_inbound_dm.apply_async(
        args=[account.id, customer.id, inbound_dm.id]
    ).get()

    assert result == "Retrying due to lock contention."
    assert calls["n"] == 3
    assert processed == [inbound_dm.id]


def test_lock_contention_exhausts_retries_and_gives_up(monkeypatch, db_session, account, customer, inbound_dm):
    calls = {"n": 0}

    def always_fails(key):
        calls["n"] += 1
        return False

    monkeypatch.setattr(task_module, "_acquire_lock", always_fails)

    result = task_module.process_inbound_dm.apply_async(
        args=[account.id, customer.id, inbound_dm.id]
    ).get()

    assert result == "Retrying due to lock contention."
    from app.tasks.webhook_processing import LOCK_MAX_RETRIES

    assert calls["n"] == LOCK_MAX_RETRIES + 1


def test_process_inbound_dm_gives_up_at_max_attempt(monkeypatch, account, customer, inbound_dm):
    """Direct, unambiguous test of the give-up branch itself (no recursion)."""
    from app.tasks.webhook_processing import LOCK_MAX_RETRIES

    monkeypatch.setattr(task_module, "_acquire_lock", lambda key: False)

    result = task_module.process_inbound_dm(
        account.id, customer.id, inbound_dm.id, 0, None, LOCK_MAX_RETRIES
    )

    assert result == "Lock contention exhausted."
