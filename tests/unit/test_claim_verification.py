from datetime import datetime, timedelta

import httpx
import pytest
import respx

import app.tasks.claim_verification as claim_verification
from app.core.config import settings
from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    Message,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import LLMService

MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
TOKEN_URL = "https://pay.tunzaa.co.tz/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/payments/initiate-payment"

# Verbatim Amara lines from the Oct 31 255754817484 chat — every one of
# these went out with no order behind it, so the gate must catch them all.
OCT31_PHANTOMS = [
    "Perfect — you're in! 🎉\n\nConfirming: *Taskflow Entry Plan* at *TZS 100,000/month*, full payment, billed monthly.\n\nYou'll get a mobile-money payment request on your phone shortly — just approve it and I start working on your shop.",
    "It's on its way to your phone — just approve the mobile-money prompt and I'll get started. 🎉",
    "You're welcome! 🙂 And once you approve it, I'll be live on your shop and ready to start closing those chats for you.",
]


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    monkeypatch.setattr(claim_verification, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000910")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Claim Shop", phone_number_id="CLAIM_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255722229999")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def context(db_session, customer):
    ctx = ConversationContext(customer_id=customer.id)
    db_session.add(ctx)
    db_session.commit()
    db_session.refresh(ctx)
    return ctx


@pytest.fixture
def product(db_session, owner):
    p = Product(
        slug="claim-plan", name="Claim Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def ig_account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_claim", access_token="ig-tok",
        username="claimshop", ai_enabled=True, user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def ig_customer(db_session, ig_account):
    c = InstagramCustomer(ig_scoped_id="igsid_claim", username="claimer", account_id=ig_account.id)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def ig_context(db_session, ig_customer):
    ctx = InstagramConversationContext(customer_id=ig_customer.id)
    db_session.add(ctx)
    db_session.commit()
    db_session.refresh(ctx)
    return ctx


def _pending_order(db_session, customer, product, account, expired=False):
    order = Order(
        customer_id=customer.id,
        product_id=product.id,
        account_id=account.id,
        quantity=1,
        agreed_price=100000,
        payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
        reserved_until=datetime.utcnow() + timedelta(hours=-2 if expired else 2),
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


# --- Caller-side gate (pure) ---


def test_gate_skips_when_this_turn_ran_the_order_machinery():
    assert (
        claim_verification.should_verify_payment_claim(
            "Lets do this",
            "A payment request for TZS 100,000 has been sent to your phone.",
            order_action=True,
        )
        is False
    )


def test_gate_flags_markers_seen_without_order_action_even_with_plain_prose():
    # The pre-#100 silent-ignore shape: marker stripped, prose sent, no order.
    assert (
        claim_verification.should_verify_payment_claim("Lets do this", "Perfect — you're in!", markers_seen=True)
        is True
    )


@pytest.mark.parametrize(
    "inbound,outbound",
    [
        ("I still haven't got the request", "It's on its way to your phone."),
        ("Okay, i'm waiting for it", "Just approve the mobile-money prompt."),
        ("Hi", "Ombi la malipo limetumwa kwenye simu yako."),
        ("Bei gani?", "TZS 100,000 kwa mwezi — lipa kwa simu."),
        ("Thanks", "You're welcome! Once you approve it, I'll be live."),
    ],
)
def test_gate_flags_payment_tokens_either_direction(inbound, outbound):
    assert claim_verification.should_verify_payment_claim(inbound, outbound) is True


def test_gate_clears_plain_sales_chat():
    assert (
        claim_verification.should_verify_payment_claim(
            "Hello",
            "Hi, I'm Amara. I help shops answer customers instantly. Mind if I ask a couple of quick questions first?",
        )
        is False
    )


@pytest.mark.parametrize("phantom", OCT31_PHANTOMS)
def test_gate_catches_oct31_phantom_lines(phantom):
    assert claim_verification.should_verify_payment_claim("Okay", phantom) is True


# --- Task: exact filters ---


def test_task_skips_on_order_action_arg_without_touching_db():
    assert (
        claim_verification.verify_payment_claim("whatsapp", "nope", "nope", None, None, True)
        == "Skipped: this turn ran the order machinery."
    )


def test_task_skips_handed_off_conversation(db_session, account, customer, context, monkeypatch):
    context.state = ConversationState.HUMAN_HANDOFF
    db_session.commit()

    async def _boom(*a, **kw):
        raise AssertionError("judge must not run for handed-off chats")

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _boom)
    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Skipped: conversation is in human handoff."
    )


def test_task_skips_when_live_pending_order_backs_the_reply(
    db_session, account, customer, context, product, monkeypatch
):
    _pending_order(db_session, customer, product, account)

    async def _boom(*a, **kw):
        raise AssertionError("judge must not run when a live order exists")

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _boom)
    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Skipped: a live pending order backs this reply."
    )


def test_task_proceeds_past_an_expired_hold(db_session, account, customer, context, product, monkeypatch):
    _pending_order(db_session, customer, product, account, expired=True)
    seen = {}

    async def _fake_judge(outbound_body, prior_paid):
        seen["ran"] = True
        return False

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Clear: no payment claim."
    )
    assert seen == {"ran": True}


def test_task_survives_internal_crash(monkeypatch):
    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(claim_verification, "SessionLocal", _boom)
    assert (
        claim_verification.verify_payment_claim("whatsapp", "x", "y", None, None) == "Error skipped."
    )


# --- Task: flag path ---


@respx.mock
def test_task_flags_phantom_and_alerts_merchant_plus_ops(
    db_session, account, customer, context, owner, monkeypatch
):
    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", "255700000999")
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append({"to": to, "template": template, "components": components})
        return {"messages": [{"id": f"wamid.alert.{to}"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "I still haven't got it", OCT31_PHANTOMS[1],
    )

    assert result == "Flagged: merchant alerted (2 send(s))."
    assert sorted(c["to"] for c in calls) == ["255700000910", "255700000999"]
    assert {c["template"] for c in calls} == {settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME}
    body_params = calls[0]["components"][1]["parameters"]
    assert customer.phone_number in body_params[1]["text"]
    assert "no order exists" in body_params[0]["text"]


@respx.mock
def test_task_stays_silent_with_no_numbers_on_file(
    db_session, account, customer, context, owner, monkeypatch
):
    owner.username = "social-login-user"
    owner.phone_number = None
    owner.handoff_phone_number = None
    db_session.commit()
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)

    async def _boom(self, *a, **kw):
        raise AssertionError("no send without recipients")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", _boom)

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Flagged: merchant alerted (0 send(s))."
    )


def test_task_throttles_repeat_alerts_for_one_conversation(
    db_session, account, customer, context, monkeypatch
):
    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    owner = account.owner
    owner.username = "social-login-user"
    owner.phone_number = None
    owner.handoff_phone_number = None
    db_session.commit()

    first = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
    )
    second = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[2],
    )
    assert first.startswith("Flagged:")
    assert second == "Throttled: already alerted for this conversation."


@respx.mock
def test_task_flags_instagram_phantom_with_at_identifier(
    db_session, ig_account, ig_customer, ig_context, monkeypatch
):
    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append({"to": to, "components": components})
        return {"messages": [{"id": "wamid.alert.ig"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    result = claim_verification.verify_payment_claim(
        "instagram", str(ig_account.id), str(ig_customer.id), "where is it?", OCT31_PHANTOMS[1],
    )

    assert result == "Flagged: merchant alerted (1 send(s))."
    assert calls[0]["components"][1]["parameters"][1]["text"] == "@claimer"


# --- Judge ---


@pytest.mark.parametrize(
    "verdict,expected",
    [("YES", True), ("yes", True), ("NO", False), ("No.", False), ("MAYBE", False), ("", False)],
)
def test_judge_parses_verdict_strictly(monkeypatch, verdict, expected):
    async def _fake_complete(provider, **kwargs):
        return verdict

    monkeypatch.setattr("app.services.llm._complete_with_retry", _fake_complete)
    monkeypatch.setattr(
        "app.services.llm.llm_service",
        type("FakeService", (), {"client": object(), "provider": object()})(),
    )
    import asyncio

    assert asyncio.run(claim_verification._judge_payment_claim("It's on its way.", False)) is expected


def test_judge_fails_silent_without_a_client(monkeypatch):
    monkeypatch.setattr("app.services.llm.llm_service", None)
    import asyncio

    assert asyncio.run(claim_verification._judge_payment_claim("It's on its way.", False)) is False


def test_judge_receives_ground_truth_facts(monkeypatch):
    seen = {}

    async def _fake_complete(provider, **kwargs):
        seen.update(kwargs)
        return "NO"

    monkeypatch.setattr("app.services.llm._complete_with_retry", _fake_complete)
    monkeypatch.setattr(
        "app.services.llm.llm_service",
        type("FakeService", (), {"client": object(), "provider": object()})(),
    )
    import asyncio

    asyncio.run(claim_verification._judge_payment_claim("Your payment went through!", True))
    assert "LIVE_PENDING=no PRIOR_PAID=yes" in seen["messages"][0]["content"]
    assert "YES or NO" in seen["system"]


# --- Turn-outcome threading ---


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


def _service_with_reply(text):
    service = LLMService(api_key=None)

    async def handler(**kwargs):
        return _FakeResponse(text)

    service.client = _FakeAnthropicClient(handler)
    return service


@respx.mock
async def test_whatsapp_close_marks_markers_seen_and_order_action(
    db_session, monkeypatch, account, customer, product
):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "TXN1"})
    )
    db_session.add(
        Message(to_phone=customer.phone_number, direction="outbound", account_id=account.id, type="text", body="hi")
    )
    db_session.commit()
    service = _service_with_reply(
        "Perfect — you're in! [AGREEMENT_REACHED: product=Claim Plan price=100000 payment=full]"
    )

    outcome: dict = {}
    reply = await service.generate_response(
        message_body="Lets do this",
        from_phone=customer.phone_number,
        db=db_session,
        account=account,
        turn_outcome=outcome,
    )

    assert "AGREEMENT_REACHED" not in reply
    assert outcome == {"markers_seen": True, "order_action": True}
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 1


@respx.mock
async def test_ignored_marker_marks_markers_seen_without_order_action(
    db_session, monkeypatch, account, customer, context, product
):
    # Agreement and handoff markers on the same turn: handoff wins, the
    # marker is ignored, no order — but the turn still saw a marker, so
    # the post-send check must look at it (it then skips on handoff state).
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")
    db_session.add(
        Message(to_phone=customer.phone_number, direction="outbound", account_id=account.id, type="text", body="hi")
    )
    db_session.commit()
    service = _service_with_reply(
        "Done! [HUMAN_HANDOFF] [AGREEMENT_REACHED: product=Claim Plan price=100000 payment=full]"
    )

    outcome: dict = {}
    await service.generate_response(
        message_body="Lets do this",
        from_phone=customer.phone_number,
        db=db_session,
        account=account,
        turn_outcome=outcome,
    )

    assert outcome == {"markers_seen": True}
    assert db_session.query(Order).filter_by(customer_id=customer.id).count() == 0


@respx.mock
async def test_instagram_close_marks_markers_seen_and_order_action(
    db_session, monkeypatch, ig_account, ig_customer, ig_context, product
):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "TXN2"})
    )
    service = _service_with_reply(
        "You're in! [AGREEMENT_REACHED: product=Claim Plan price=100000 payment=full phone=255722221111]"
    )

    outcome: dict = {}
    reply = await service.generate_dm_response(
        message_body="Lets do this",
        db=db_session,
        account=ig_account,
        customer=ig_customer,
        context=ig_context,
        conversation_history=[],
        turn_outcome=outcome,
    )

    assert "AGREEMENT_REACHED" not in reply
    assert outcome == {"markers_seen": True, "order_action": True}
    assert (
        db_session.query(Order).filter_by(instagram_customer_id=ig_customer.id).count() == 1
    )


# --- Post-send enqueue (full loop) ---


@respx.mock
def test_whatsapp_pipeline_enqueues_verify_and_alerts_on_phantom(
    db_session, monkeypatch, account, customer, context
):
    import app.tasks.webhook_processing as webhook_processing

    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.claim1"}]})
    )

    async def fake_generate_response(**kwargs):
        return OCT31_PHANTOMS[1]

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    monkeypatch.setattr(webhook_processing, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(to)
        return {"messages": [{"id": "wamid.alert.pipe"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    original = webhook_processing.llm_service
    webhook_processing.llm_service = FakeLLMService()
    try:
        result = webhook_processing.process_inbound_message(
            str(account.id), customer.phone_number, "I still haven't got the request"
        )
    finally:
        webhook_processing.llm_service = original

    assert result == "Success"
    assert calls == ["255700000910"]


@respx.mock
def test_instagram_pipeline_enqueues_verify_and_alerts_on_phantom(
    db_session, monkeypatch, ig_account, ig_customer, ig_context
):
    import app.tasks.instagram_dm_processing as dm_processing

    dm = InstagramDirectMessage(
        account_id=ig_account.id,
        customer_id=ig_customer.id,
        direction="inbound",
        body="I still haven't got the request",
        ig_message_id="mid_claim1",
    )
    db_session.add(dm)
    db_session.commit()
    db_session.refresh(dm)

    async def fake_generate_dm_response(**kwargs):
        return OCT31_PHANTOMS[1]

    class FakeLLMService:
        generate_dm_response = staticmethod(fake_generate_dm_response)

    class FakeProvider:
        def __init__(self, access_token, response=None):
            pass

        async def send_direct_message(self, ig_business_id, recipient_id, message):
            return {"message_id": "mid_out_claim1"}

    async def _fake_judge(outbound_body, prior_paid):
        return True

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _fake_judge)
    monkeypatch.setattr(dm_processing, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(dm_processing, "InstagramProvider", FakeProvider)
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(to)
        return {"messages": [{"id": "wamid.alert.igpipe"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    original = dm_processing.llm_service
    dm_processing.llm_service = FakeLLMService()
    try:
        result = dm_processing.process_inbound_dm(
            str(ig_account.id), str(ig_customer.id), str(dm.id)
        )
    finally:
        dm_processing.llm_service = original

    assert result == "Processed."
    assert calls == ["255700000910"]
