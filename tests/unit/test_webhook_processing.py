import httpx
import pytest
import respx

import app.tasks.webhook_processing as webhook_processing
from app.db.models import Message, User, WhatsAppAccount

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """process_inbound_message calls the module-level SessionLocal directly."""
    monkeypatch.setattr(webhook_processing, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000900")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Task Test Shop", phone_number_id="TASK_PHONE", access_token="tok",
        ai_enabled=True, user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


def test_process_inbound_message_missing_account():
    result = webhook_processing.process_inbound_message(999999, "255711111111", "hi")

    assert result == "Account not found."


@respx.mock
def test_quota_exhausted_sends_holding_without_llm(db_session, account, owner, monkeypatch):
    """An over-limit plan gets the holding message: no model call, and the
    holding send itself is never metered (#90)."""
    from datetime import datetime, timedelta

    import app.tasks.usage_alerts as usage_alerts
    from app.db.models import (
        AmaraEvent, AmaraEventKind, Customer, Order, OrderStatus, PaymentType,
        Product, ProductSubscription, ProductSubscriptionStatus,
    )
    from app.services import usage

    seller = User(username="255700000901")
    db_session.add(seller)
    db_session.commit()
    db_session.refresh(seller)
    plan = Product(
        slug="plan-quota", name="Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=seller.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2,
    )
    customer = Customer(phone_number=owner.username)
    db_session.add_all([plan, customer])
    db_session.commit()
    db_session.refresh(plan)
    db_session.refresh(customer)
    order = Order(
        customer_id=customer.id, product_id=plan.id, quantity=1, agreed_price=100000,
        payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.add(ProductSubscription(
        order_id=order.id, customer_id=customer.id, product_id=plan.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        next_billing_at=datetime.utcnow() + timedelta(days=5),
    ))
    db_session.commit()
    monkeypatch.setattr(usage_alerts.send_usage_warning, "apply_async", lambda args, **kw: None)
    usage.record_reply(db_session, owner.id, "whatsapp")
    usage.record_reply(db_session, owner.id, "whatsapp")

    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.quota1"}]})
    )
    llm_calls = []

    async def fake_generate_response(**kwargs):
        llm_calls.append(kwargs)
        return "Hello!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    original = webhook_processing.llm_service
    webhook_processing.llm_service = FakeLLMService()
    try:
        result = webhook_processing.process_inbound_message(account.id, "255711111111", "hi")
    finally:
        webhook_processing.llm_service = original

    assert result == "Success"
    assert llm_calls == []
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound.body == usage.QUOTA_EXHAUSTED_HOLDING_MESSAGE
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).count() == 2
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.LIMIT_BLOCKED).count() == 1


@respx.mock
def test_process_inbound_message_direct_call_runs_ai_path(db_session, account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.direct1"}]})
    )

    async def fake_generate_response(**kwargs):
        return "Hello!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    original = webhook_processing.llm_service
    webhook_processing.llm_service = FakeLLMService()
    try:
        # Called directly (not via apply_async) — exercises _run_async's
        # plain asyncio.run() path, since a bare pytest test function has
        # no event loop already running in this thread.
        result = webhook_processing.process_inbound_message(
            account.id, "255722223333", "Hi there"
        )
    finally:
        webhook_processing.llm_service = original

    assert result == "Success"
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound.body == "Hello!"


# --- Per-customer lock --------------------------------------------------------
#
# Lock contention re-dispatches via apply_async(countdown=...) with `attempt`
# incremented, rather than Celery's self.retry() — under task_always_eager
# (this test suite's config) retry() just raises immediately instead of
# looping, since it only really re-queues through an actual broker. A plain
# recursive apply_async runs synchronously to completion under eager mode
# (ignoring countdown), so these tests can assert on the final outcome of a
# single top-level call without needing to fake broker/worker scheduling.

def test_lock_contention_retries_then_succeeds(monkeypatch, db_session, account):
    calls = {"n": 0}
    processed = []
    real_acquire = webhook_processing._acquire_lock

    def flaky_acquire(key):
        calls["n"] += 1
        if calls["n"] < 3:
            return False
        return real_acquire(key)

    monkeypatch.setattr(webhook_processing, "_acquire_lock", flaky_acquire)

    async def fake_process(db, acct, from_phone, body, **kwargs):
        processed.append(from_phone)
        return "Success"

    monkeypatch.setattr(webhook_processing, "_process", fake_process)

    # The top-level call returns as soon as *it* dispatches its own retry —
    # it doesn't wait on nested retries, matching production (a retry is a
    # genuinely separate, later broker message the original caller never
    # sees the outcome of). Under eager mode the nested apply_async calls
    # still all run to completion synchronously before this returns, so the
    # eventual success is observable via calls made / side effects, not the
    # top-level return value.
    result = webhook_processing.process_inbound_message.apply_async(
        args=[account.id, "255733334444", "hi"]
    ).get()

    assert result == "Retrying due to lock contention."
    assert calls["n"] == 3
    assert processed == ["255733334444"]


def test_lock_contention_exhausts_retries_and_gives_up(monkeypatch, db_session, account):
    calls = {"n": 0}

    def always_fails(key):
        calls["n"] += 1
        return False

    monkeypatch.setattr(webhook_processing, "_acquire_lock", always_fails)

    result = webhook_processing.process_inbound_message.apply_async(
        args=[account.id, "255744445555", "hi"]
    ).get()

    # Same reasoning as above: the top-level result is always "Retrying..."
    # since attempt 0 never itself exceeds the max — the giving-up branch
    # only ever fires on the innermost (attempt == LOCK_MAX_RETRIES)
    # nested call, whose return value nothing waits on. Assert the full
    # attempt chain ran instead (initial call + one per retry).
    assert result == "Retrying due to lock contention."
    assert calls["n"] == webhook_processing.LOCK_MAX_RETRIES + 1


def test_process_inbound_message_gives_up_at_max_attempt(monkeypatch, account):
    """Direct, unambiguous test of the give-up branch itself (no recursion)."""
    monkeypatch.setattr(webhook_processing, "_acquire_lock", lambda key: False)

    result = webhook_processing.process_inbound_message(
        account.id, "255744445555", "hi", attempt=webhook_processing.LOCK_MAX_RETRIES
    )

    assert result == "Lock contention exhausted."


def test_acquire_lock_fails_open_on_redis_error(monkeypatch):
    class BoomClient:
        def set(self, *a, **kw):
            raise Exception("redis down")

    monkeypatch.setattr(webhook_processing.rate_limit_module, "_redis_client", BoomClient())

    assert webhook_processing._acquire_lock("some-key") is True


def test_release_lock_swallows_redis_error(monkeypatch):
    class BoomClient:
        def delete(self, *a, **kw):
            raise Exception("redis down")

    monkeypatch.setattr(webhook_processing.rate_limit_module, "_redis_client", BoomClient())

    # Should not raise.
    webhook_processing._release_lock("some-key")


# --- AI reply rate limiting ---------------------------------------------------

@respx.mock
def test_ai_reply_rate_limit_blocks_after_threshold(monkeypatch, db_session, account):
    monkeypatch.setattr(webhook_processing, "AI_REPLY_RATE_LIMIT", 1)

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.rl1"}]})
    )

    async def fake_generate_response(**kwargs):
        return "Reply"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    account_id = account.id  # captured before any call, since the first
    # call's `finally: db.close()` (shared session in tests) would detach
    # `account` before a second `account.id` access could run.

    original = webhook_processing.llm_service
    webhook_processing.llm_service = FakeLLMService()
    try:
        first = webhook_processing.process_inbound_message(account_id, "255755556666", "hi")
        second = webhook_processing.process_inbound_message(account_id, "255755556666", "hi again")
    finally:
        webhook_processing.llm_service = original

    assert first == "Success"
    assert second == "Rate limited."
    assert send_route.call_count == 1
    assert db_session.query(Message).filter(Message.direction == "outbound").count() == 1


# --- Claude-outage and send retries (#38) --------------------------------------
#
# Same eager-mode recursion as the lock tests above: an outage re-dispatch
# runs inline (countdown ignored), so a single top-level call plays out the
# whole retry sequence.

def _fake_llm(monkeypatch, generate_response, alerts=None):
    class FakeLLMService:
        pass

    FakeLLMService.generate_response = staticmethod(generate_response)

    async def record_alert(db, merchant, reply_identifier, customer_message, alert_target=None):
        alerts.append((reply_identifier, customer_message))

    FakeLLMService._send_platform_alert_to_merchant = staticmethod(record_alert)
    monkeypatch.setattr(webhook_processing, "llm_service", FakeLLMService())


@respx.mock
def test_llm_outage_retries_then_replies(monkeypatch, db_session, account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.retry1"}]})
    )
    calls = []

    async def flaky(**kwargs):
        calls.append(kwargs["retry_on_outage"])
        if len(calls) < 3:
            raise webhook_processing.LLMUnavailable("overloaded")
        return "Hello!"

    _fake_llm(monkeypatch, flaky)
    account_id = account.id  # the task's db.close() detaches `account`

    webhook_processing.process_inbound_message.apply_async(args=[account_id, "255744440001", "hi"])

    assert calls == [True, True, True]
    outbound = db_session.query(Message).filter(Message.direction == "outbound").all()
    assert [m.body for m in outbound] == ["Hello!"]
    # Retries are the same message: counted against the rate limit once.
    key = f"ai_reply:{account_id}:255744440001"
    assert int(webhook_processing.rate_limit_module._redis_client.get(key)) == 1


@respx.mock
def test_llm_outage_exhausted_last_attempt_does_not_retry(monkeypatch, db_session, account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.retry2"}]})
    )
    calls = []

    async def always_down(**kwargs):
        calls.append(kwargs["retry_on_outage"])
        if kwargs["retry_on_outage"]:
            raise webhook_processing.LLMUnavailable("overloaded")
        return "outage reply"

    _fake_llm(monkeypatch, always_down)

    webhook_processing.process_inbound_message.apply_async(args=[account.id, "255744440002", "hi"])

    # One try per countdown, then a final attempt that must not raise.
    assert calls == [True] * len(webhook_processing.LLM_RETRY_COUNTDOWNS_SECONDS) + [False]
    outbound = db_session.query(Message).filter(Message.direction == "outbound").all()
    assert [m.body for m in outbound] == ["outage reply"]


@respx.mock
def test_transient_send_failure_is_retried(monkeypatch, db_session, account):
    monkeypatch.setattr(webhook_processing, "SEND_RETRY_DELAYS_SECONDS", (0, 0))
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(500, json={"error": {"code": 2, "message": "Service unavailable"}}),
            httpx.Response(200, json={"messages": [{"id": "wamid.retry3"}]}),
        ]
    )

    async def reply(**kwargs):
        return "Hello!"

    _fake_llm(monkeypatch, reply)

    result = webhook_processing.process_inbound_message(account.id, "255744440003", "hi")

    assert result == "Success"
    assert route.call_count == 3
    outbound = db_session.query(Message).filter(Message.direction == "outbound").one()
    assert outbound.status == "sent"


@respx.mock
def test_permanent_send_failure_not_retried_and_alerts_merchant(monkeypatch, db_session, account):
    monkeypatch.setattr(webhook_processing, "SEND_RETRY_DELAYS_SECONDS", (0, 0))
    route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
    )
    alerts = []

    async def reply(**kwargs):
        return "Hello!"

    _fake_llm(monkeypatch, reply, alerts)

    webhook_processing.process_inbound_message(account.id, "255744440004", "do you have size 42?")

    assert route.call_count == 1
    assert db_session.query(Message).filter(Message.direction == "outbound").one().status == "failed"
    assert alerts == [("255744440004", "do you have size 42?")]


# --- Reply metering (#41) ---------------------------------------------------------

def _replies(db_session):
    from app.db.models import AmaraEvent, AmaraEventKind

    return db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).all()


@respx.mock
def test_delivered_ai_reply_is_metered(monkeypatch, db_session, account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.meter1"}]})
    )

    async def reply(**kwargs):
        return "Hello!"

    _fake_llm(monkeypatch, reply)
    owner_id, account_id = account.user_id, account.id

    webhook_processing.process_inbound_message(account_id, "255744450001", "hi")

    replies = _replies(db_session)
    assert [(r.merchant_id, r.channel, r.account_id) for r in replies] == [(owner_id, "whatsapp", account_id)]


@respx.mock
def test_undelivered_ai_reply_is_not_metered(monkeypatch, db_session, account):
    monkeypatch.setattr(webhook_processing, "SEND_RETRY_DELAYS_SECONDS", (0, 0))
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
    )

    async def reply(**kwargs):
        return "Hello!"

    _fake_llm(monkeypatch, reply, alerts=[])

    webhook_processing.process_inbound_message(account.id, "255744450002", "hi")

    assert _replies(db_session) == []


@respx.mock
def test_merchant_command_reply_is_not_metered(monkeypatch, db_session, account, owner):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.meter3"}]})
    )

    webhook_processing.process_inbound_message(account.id, owner.username, "/help")

    assert _replies(db_session) == []
