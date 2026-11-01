from datetime import datetime, timedelta

import httpx
import pytest
import respx

import app.tasks.claim_verification as claim_verification
from app.core.config import settings
from app.db.models import (
    AmaraEventKind,
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
from app.services import usage as usage_service
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


def _verdict(**over):
    base = {"claim": True, "product": None, "price": None, "consent": True, "phone": None}
    base.update(over)
    return base


def _judge_returning(verdict):
    async def _fake_judge(history, prior_paid):
        return verdict

    return _fake_judge


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

    async def _fake_judge(history, prior_paid):
        seen["ran"] = True
        return _verdict(claim=False, consent=False)

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
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", "255700000999")
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append({"to": to, "template": template, "components": components})
        return {"messages": [{"id": f"wamid.alert.{to}"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    # Capture scalars before the call: the task commits and closes the
    # session, detaching these fixtures.
    phone = customer.phone_number
    context_id = context.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), phone, "I still haven't got it", OCT31_PHANTOMS[1],
    )

    assert result == "Flagged: handed off without recovery (no_terms), merchant alerted (2 send(s))."
    assert sorted(c["to"] for c in calls) == ["255700000910", "255700000999"]
    assert {c["template"] for c in calls} == {settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME}
    body_params = calls[0]["components"][1]["parameters"]
    assert phone in body_params[1]["text"]
    assert "no order behind it" in body_params[0]["text"]
    assert "Chat handed to you" in body_params[0]["text"]
    assert db_session.get(ConversationContext, context_id).state == ConversationState.HUMAN_HANDOFF


@respx.mock
def test_task_stays_silent_with_no_numbers_on_file(
    db_session, account, customer, context, owner, monkeypatch
):
    owner.username = "social-login-user"
    owner.phone_number = None
    owner.handoff_phone_number = None
    db_session.commit()
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))

    async def _boom(self, *a, **kw):
        raise AssertionError("no send without recipients")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", _boom)

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Flagged: handed off without recovery (no_terms), merchant alerted (0 send(s))."
    )


def test_task_throttles_repeat_alerts_for_one_conversation(
    db_session, account, customer, context, monkeypatch
):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    owner = account.owner
    owner.username = "social-login-user"
    owner.phone_number = None
    owner.handoff_phone_number = None
    db_session.commit()

    phone = customer.phone_number
    context_id = context.id
    first = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), phone, "where is it?", OCT31_PHANTOMS[1],
    )
    # The first flag hands the chat off; reopen it so the second call
    # reaches the throttle instead of the handoff skip.
    db_session.get(ConversationContext, context_id).state = ConversationState.NEGOTIATING
    db_session.commit()
    second = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), phone, "where is it?", OCT31_PHANTOMS[2],
    )
    assert first.startswith("Flagged:")
    assert second == "Throttled: already alerted for this conversation."


@respx.mock
def test_task_flags_instagram_phantom_with_at_identifier(
    db_session, ig_account, ig_customer, ig_context, monkeypatch
):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append({"to": to, "components": components})
        return {"messages": [{"id": "wamid.alert.ig"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    ig_customer_id = str(ig_customer.id)
    ig_context_id = ig_context.id
    result = claim_verification.verify_payment_claim(
        "instagram", str(ig_account.id), ig_customer_id, "where is it?", OCT31_PHANTOMS[1],
    )

    assert result == "Flagged: handed off without recovery (no_terms), merchant alerted (1 send(s))."
    assert calls[0]["components"][1]["parameters"][1]["text"] == "@claimer"
    assert (
        db_session.get(InstagramConversationContext, ig_context_id).state
        == ConversationState.HUMAN_HANDOFF
    )


# --- Judge ---


_FULL_YES = "CLAIM: YES\nPRODUCT: Claim Plan\nPRICE: 100000\nCONSENT: YES\nPHONE: NONE"
_FULL_YES_DICT = {"claim": True, "product": "Claim Plan", "price": 100000, "consent": True, "phone": None}
_FULL_NO_DICT = {"claim": False, "product": None, "price": None, "consent": False, "phone": None}


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (_FULL_YES, _FULL_YES_DICT),
        ("claim: no\nproduct: none\nprice: none\nconsent: no\nphone: none", _FULL_NO_DICT),
        # Reasoning-model narration around the five lines is tolerated.
        ("Here is my verdict:\n" + _FULL_YES + "\nDone.", _FULL_YES_DICT),
        ("CLAIM: YES\nPRODUCT: X\nPRICE: 100\nCONSENT: YES", None),  # missing PHONE
        ("CLAIM: MAYBE\nPRODUCT: NONE\nPRICE: NONE\nCONSENT: YES\nPHONE: NONE", None),
        (
            "CLAIM: YES\nPRODUCT: X\nPRICE: 100,000\nCONSENT: YES\nPHONE: NONE",
            {"claim": True, "product": "X", "price": 100000, "consent": True, "phone": None},
        ),
        ("CLAIM: YES\nPRODUCT: X\nPRICE: lots\nCONSENT: YES\nPHONE: NONE", None),
        ("", None),
    ],
)
def test_judge_parses_verdict_strictly(monkeypatch, verdict, expected):
    async def _fake_complete(provider, **kwargs):
        return verdict

    monkeypatch.setattr("app.services.llm._complete_with_retry", _fake_complete)
    monkeypatch.setattr(
        "app.services.llm._default_llm_service",
        lambda: type("FakeService", (), {"client": object(), "provider": object()})(),
    )
    import asyncio

    assert (
        asyncio.run(claim_verification._judge_payment_claim([("outbound", "It's on its way.")], False))
        == expected
    )


def test_judge_fails_closed_without_a_client(monkeypatch):
    monkeypatch.setattr("app.services.llm._default_llm_service", lambda: None)
    import asyncio

    assert asyncio.run(claim_verification._judge_payment_claim([("outbound", "It's on its way.")], False)) is None


def test_judge_receives_ground_truth_facts_and_transcript(monkeypatch):
    seen = {}

    async def _fake_complete(provider, **kwargs):
        seen.update(kwargs)
        return "CLAIM: NO\nPRODUCT: NONE\nPRICE: NONE\nCONSENT: NO\nPHONE: NONE"

    monkeypatch.setattr("app.services.llm._complete_with_retry", _fake_complete)
    monkeypatch.setattr(
        "app.services.llm._default_llm_service",
        lambda: type("FakeService", (), {"client": object(), "provider": object()})(),
    )
    import asyncio

    asyncio.run(
        claim_verification._judge_payment_claim(
            [("inbound", "where is it?"), ("outbound", "Your payment went through!")], True
        )
    )
    content = seen["messages"][0]["content"]
    assert "LIVE_PENDING=no PRIOR_PAID=yes" in content
    assert "buyer: where is it?" in content
    assert "amara: Your payment went through!" in content
    assert "CLAIM:" in seen["system"]


# --- Fail-open and defensive branches ---


def test_throttle_fails_open_on_redis_outage(monkeypatch):
    class _BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis down")

        def set(self, *args, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.rate_limit._redis_client", _BrokenRedis())
    assert claim_verification._throttle_hit("k") is False
    assert claim_verification._throttle_set("k") is True


def test_judge_fails_closed_when_complete_crashes(monkeypatch):
    async def _fake_complete(provider, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.services.llm._complete_with_retry", _fake_complete)
    monkeypatch.setattr(
        "app.services.llm._default_llm_service",
        lambda: type("FakeService", (), {"client": object(), "provider": object()})(),
    )
    import asyncio

    assert asyncio.run(claim_verification._judge_payment_claim([("outbound", "It's on its way.")], False)) is None


@respx.mock
def test_task_survives_alert_send_failure(db_session, account, customer, context, monkeypatch):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _boom(self, *a, **kw):
        raise RuntimeError("meta down")

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_template", _boom)

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Flagged: handed off without recovery (no_terms), merchant alerted (0 send(s))."
    )


@respx.mock
def test_task_survives_alert_rejection(db_session, account, customer, context, monkeypatch):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _fake_send_template(self, *a, **kw):
        return {"error": {"message": "rejected"}}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Flagged: handed off without recovery (no_terms), merchant alerted (0 send(s))."
    )


def test_task_unknown_account_id():
    assert (
        claim_verification.verify_payment_claim("whatsapp", "not-a-uuid", "x", None, None)
        == "Account not found."
    )


def test_task_unknown_channel():
    import uuid

    assert (
        claim_verification.verify_payment_claim("sms", str(uuid.uuid4()), "x", None, None)
        == "Unknown channel."
    )


def test_task_missing_customer(db_session, account):
    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), "255700000000", "where is it?", OCT31_PHANTOMS[1],
        )
        == "Account, customer, or conversation not found."
    )


def test_task_throttled_on_lost_set_race(db_session, account, customer, context, monkeypatch):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
    monkeypatch.setattr(claim_verification, "_throttle_hit", lambda key: False)
    monkeypatch.setattr(claim_verification, "_throttle_set", lambda key: False)

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
        )
        == "Throttled: alert already sent."
    )


@respx.mock
@pytest.mark.parametrize("long", [False, True])
def test_task_truncates_alert_copy_at_350(db_session, account, customer, context, monkeypatch, long):
    monkeypatch.setattr(
        claim_verification, "_judge_payment_claim", _judge_returning(_verdict(consent=False))
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.long"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    phantom = (
        "It's on its way to your phone, please approve it now. " * 10
        if long
        else OCT31_PHANTOMS[1]
    )
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "where is it?", phantom,
    )

    assert result == "Flagged: handed off without recovery (no_consent), merchant alerted (1 send(s))."
    flagged_text = calls[0][1]["parameters"][0]["text"]
    if long:
        assert phantom not in flagged_text
        assert len(flagged_text) <= 350
        assert flagged_text.endswith("...")
    else:
        assert phantom in flagged_text
        assert flagged_text.endswith('".')


@respx.mock
def test_whatsapp_pipeline_survives_verify_enqueue_failure(
    db_session, monkeypatch, account, customer, context
):
    import app.tasks.webhook_processing as webhook_processing

    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.claim2"}]})
    )

    async def fake_generate_response(**kwargs):
        return OCT31_PHANTOMS[1]

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    def _boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(claim_verification.verify_payment_claim, "apply_async", _boom)

    original = webhook_processing.llm_service
    webhook_processing.llm_service = FakeLLMService()
    try:
        result = webhook_processing.process_inbound_message(
            str(account.id), customer.phone_number, "I still haven't got the request"
        )
    finally:
        webhook_processing.llm_service = original

    assert result == "Success"


@respx.mock
def test_instagram_pipeline_survives_verify_enqueue_failure(
    db_session, monkeypatch, ig_account, ig_customer, ig_context
):
    import app.tasks.instagram_dm_processing as dm_processing

    dm = InstagramDirectMessage(
        account_id=ig_account.id,
        customer_id=ig_customer.id,
        direction="inbound",
        body="I still haven't got the request",
        ig_message_id="mid_claim2",
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
            return {"message_id": "mid_out_claim2"}

    def _boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(claim_verification.verify_payment_claim, "apply_async", _boom)
    monkeypatch.setattr(dm_processing, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(dm_processing, "InstagramProvider", FakeProvider)

    original = dm_processing.llm_service
    dm_processing.llm_service = FakeLLMService()
    try:
        result = dm_processing.process_inbound_dm(
            str(ig_account.id), str(ig_customer.id), str(dm.id)
        )
    finally:
        dm_processing.llm_service = original

    assert result == "Processed."


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

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
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

    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(_verdict()))
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


# --- #120: actionable flags (handoff + recover-from-terms) ---


@respx.mock
def test_task_recovers_order_from_terms_and_alerts(
    db_session, account, customer, context, product, owner, monkeypatch
):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "TXN9"})
    )
    monkeypatch.setattr("app.services.llm.llm_service", LLMService(api_key=None))
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=100000)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.recovered"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    customer_id = customer.id
    context_id = context.id
    owner_id = owner.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "Lets do this", OCT31_PHANTOMS[0],
    )

    assert result == "Flagged: recovered Claim Plan order, merchant alerted (1 send(s))."
    fresh = db_session.query(Order).filter_by(customer_id=customer_id).one()
    assert fresh.status == OrderStatus.PENDING
    assert fresh.agreed_price == 100000
    assert db_session.get(ConversationContext, context_id).state == ConversationState.HUMAN_HANDOFF
    copy = calls[0][1]["parameters"][0]["text"]
    assert "recreated it" in copy
    assert "TZS 100,000" in copy
    now = datetime.utcnow()
    assert (
        usage_service.count_events(
            db_session, owner_id, AmaraEventKind.HANDOFF,
            now - timedelta(hours=1), now + timedelta(hours=1),
        )
        == 1
    )


@respx.mock
def test_task_handoff_only_when_no_consent(db_session, account, customer, context, monkeypatch):
    monkeypatch.setattr(
        claim_verification, "_judge_payment_claim", _judge_returning(_verdict(consent=False))
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _fake_send_template(self, *a, **kw):
        return {"messages": [{"id": "wamid.alert.noconsent"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    customer_id = customer.id
    context_id = context.id
    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "stop", OCT31_PHANTOMS[1],
        )
        == "Flagged: handed off without recovery (no_consent), merchant alerted (1 send(s))."
    )
    assert db_session.query(Order).filter_by(customer_id=customer_id).count() == 0
    assert db_session.get(ConversationContext, context_id).state == ConversationState.HUMAN_HANDOFF


@respx.mock
def test_task_handoff_only_when_product_unknown(
    db_session, account, customer, context, product, monkeypatch
):
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Ghost", price=50000)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.unknown"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    customer_id = customer.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "Lets do this", OCT31_PHANTOMS[0],
    )

    assert result == "Flagged: handed off without recovery (no_terms), merchant alerted (1 send(s))."
    assert "no product matching 'Ghost'" in calls[0][1]["parameters"][0]["text"]
    assert db_session.query(Order).filter_by(customer_id=customer_id).count() == 0


@respx.mock
def test_task_handoff_only_when_price_outside_band(
    db_session, account, customer, context, product, monkeypatch
):
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=50000)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.band"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    customer_id = customer.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "Lets do this", OCT31_PHANTOMS[0],
    )

    assert result == "Flagged: handed off without recovery (no_terms), merchant alerted (1 send(s))."
    assert "outside 'Claim Plan' band" in calls[0][1]["parameters"][0]["text"]
    assert db_session.query(Order).filter_by(customer_id=customer_id).count() == 0


@respx.mock
def test_task_handoff_only_when_intent_disagrees(
    db_session, account, customer, context, product, monkeypatch
):
    context.current_product_intent = "Other Plan"
    db_session.commit()
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=100000)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.intent"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    customer_id = customer.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "Lets do this", OCT31_PHANTOMS[0],
    )

    assert result == "Flagged: handed off without recovery (no_terms), merchant alerted (1 send(s))."
    assert "disagrees" in calls[0][1]["parameters"][0]["text"]
    assert db_session.query(Order).filter_by(customer_id=customer_id).count() == 0


@respx.mock
def test_task_handoff_when_judge_unavailable(db_session, account, customer, context, monkeypatch):
    monkeypatch.setattr(claim_verification, "_judge_payment_claim", _judge_returning(None))
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.judgedown"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    context_id = context.id
    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "where is it?", OCT31_PHANTOMS[1],
    )

    assert result == (
        "Flagged: handed off without recovery (judge_unavailable), merchant alerted (1 send(s))."
    )
    assert "checker was down" in calls[0][1]["parameters"][0]["text"]
    assert db_session.get(ConversationContext, context_id).state == ConversationState.HUMAN_HANDOFF


@respx.mock
def test_task_recovers_instagram_order_with_buyer_phone(
    db_session, ig_account, ig_customer, ig_context, product, monkeypatch
):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"statusCode": 202, "success": True, "transactionID": "TXN10"})
    )
    monkeypatch.setattr("app.services.llm.llm_service", LLMService(api_key=None))
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=100000, phone="255722221111")),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _fake_send_template(self, *a, **kw):
        return {"messages": [{"id": "wamid.alert.igrecovered"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    ig_customer_id = ig_customer.id
    ig_context_id = ig_context.id
    result = claim_verification.verify_payment_claim(
        "instagram", str(ig_account.id), str(ig_customer_id), "tuma ombi", OCT31_PHANTOMS[0],
    )

    assert result == "Flagged: recovered Claim Plan order, merchant alerted (1 send(s))."
    fresh = db_session.query(Order).filter_by(instagram_customer_id=ig_customer_id).one()
    assert fresh.status == OrderStatus.PENDING
    assert (
        db_session.get(InstagramConversationContext, ig_context_id).state
        == ConversationState.HUMAN_HANDOFF
    )


@respx.mock
@pytest.mark.parametrize("phone", [None, "abc"])
def test_task_instagram_handoff_only_without_phone(
    db_session, ig_account, ig_customer, ig_context, product, monkeypatch, phone
):
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=100000, phone=phone)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    async def _fake_send_template(self, *a, **kw):
        return {"messages": [{"id": "wamid.alert.igonophone"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    ig_customer_id = ig_customer.id
    assert (
        claim_verification.verify_payment_claim(
            "instagram", str(ig_account.id), str(ig_customer_id), "tuma ombi", OCT31_PHANTOMS[0],
        )
        == "Flagged: handed off without recovery (no_phone), merchant alerted (1 send(s))."
    )
    assert db_session.query(Order).filter_by(instagram_customer_id=ig_customer_id).count() == 0


@respx.mock
@pytest.mark.parametrize(
    "reason,snippet",
    [("out_of_stock", "out of stock"), ("weird", "Recovery failed")],
)
def test_task_recovery_failure_maps_reason(
    db_session, account, customer, context, product, monkeypatch, reason, snippet
):
    async def _fake_close(db, parties, prod, price):
        return None, reason

    monkeypatch.setattr("app.services.payment_resend.close_deal_from_terms", _fake_close)
    monkeypatch.setattr(
        claim_verification,
        "_judge_payment_claim",
        _judge_returning(_verdict(product="Claim Plan", price=100000)),
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)
    calls = []

    async def _fake_send_template(self, to, template, language_code=None, components=None):
        calls.append(components)
        return {"messages": [{"id": "wamid.alert.failmap"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    result = claim_verification.verify_payment_claim(
        "whatsapp", str(account.id), customer.phone_number, "Lets do this", OCT31_PHANTOMS[0],
    )

    assert result == (
        f"Flagged: handed off without recovery (recover_{reason}), merchant alerted (1 send(s))."
    )
    assert snippet in calls[0][1]["parameters"][0]["text"]


@respx.mock
def test_task_survives_handoff_reminder_schedule_failure(
    db_session, account, customer, context, monkeypatch
):
    monkeypatch.setattr(
        claim_verification, "_judge_payment_claim", _judge_returning(_verdict(consent=False))
    )
    monkeypatch.setattr(settings, "OPS_ALERT_PHONE", None)

    def _boom(*a, **kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        "app.tasks.handoff_reminders.send_handoff_reminder.apply_async", _boom
    )

    async def _fake_send_template(self, *a, **kw):
        return {"messages": [{"id": "wamid.alert.remfail"}]}

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_template", _fake_send_template
    )

    assert (
        claim_verification.verify_payment_claim(
            "whatsapp", str(account.id), customer.phone_number, "stop", OCT31_PHANTOMS[1],
        )
        == "Flagged: handed off without recovery (no_consent), merchant alerted (1 send(s))."
    )


def test_recent_history_whatsapp_filters_orders_and_caps(db_session, account, customer):
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(8):
        inbound = i % 2 == 0
        db_session.add(
            Message(
                account_id=account.id,
                direction="inbound" if inbound else "outbound",
                from_phone=customer.phone_number if inbound else "255700000001",
                to_phone="255700000001" if inbound else customer.phone_number,
                type="text",
                body=f"turn {i}",
                timestamp=base + timedelta(minutes=i),
            )
        )
    # Same buyer, trunk-prefix format — must still match.
    db_session.add(
        Message(
            account_id=account.id,
            direction="inbound",
            from_phone="0722229999",
            to_phone="255700000001",
            type="text",
            body="trunk format",
            timestamp=base + timedelta(minutes=8),
        )
    )
    # Noise: another buyer, and a non-text row.
    db_session.add(
        Message(
            account_id=account.id,
            direction="inbound",
            from_phone="255700000002",
            to_phone="255700000001",
            type="text",
            body="stranger",
            timestamp=base + timedelta(minutes=9),
        )
    )
    db_session.add(
        Message(
            account_id=account.id,
            direction="inbound",
            from_phone=customer.phone_number,
            to_phone="255700000001",
            type="image",
            body="[image]",
            timestamp=base + timedelta(minutes=10),
        )
    )
    db_session.commit()

    history = claim_verification._recent_history(db_session, "whatsapp", account.id, customer, None)

    assert [body for _, body in history] == [f"turn {i}" for i in range(3, 8)] + ["trunk format"]
    assert [direction for direction, _ in history] == [
        "outbound", "inbound", "outbound", "inbound", "outbound", "inbound",
    ]


def test_recent_history_instagram_chronological(db_session, ig_account, ig_customer):
    other = InstagramCustomer(ig_scoped_id="igsid_other", username="other", account_id=ig_account.id)
    db_session.add(other)
    db_session.commit()
    base = datetime(2026, 1, 1, 12, 0, 0)
    bodies = ["hello?", "still waiting", "tuma ombi"]
    for i, body in enumerate(bodies):
        db_session.add(
            InstagramDirectMessage(
                account_id=ig_account.id,
                customer_id=ig_customer.id,
                direction="inbound",
                body=body,
                created_at=base + timedelta(minutes=i),
            )
        )
    db_session.add(
        InstagramDirectMessage(
            account_id=ig_account.id,
            customer_id=ig_customer.id,
            direction="inbound",
            body=None,
            created_at=base + timedelta(minutes=9),
        )
    )
    db_session.add(
        InstagramDirectMessage(
            account_id=ig_account.id,
            customer_id=other.id,
            direction="inbound",
            body="someone else",
            created_at=base + timedelta(minutes=10),
        )
    )
    db_session.commit()

    history = claim_verification._recent_history(db_session, "instagram", ig_account.id, ig_customer, None)

    assert history == [("inbound", body) for body in bodies]


def test_recent_history_falls_back_to_outbound_body(db_session, account, customer):
    assert claim_verification._recent_history(
        db_session, "whatsapp", account.id, customer, "fallback reply"
    ) == [("outbound", "fallback reply")]
    assert claim_verification._recent_history(db_session, "whatsapp", account.id, customer, None) == []


def test_resolve_terms_defaults_price_to_max(db_session, account, context, product):
    resolved, price, why = claim_verification._resolve_terms(
        db_session, account, context, _verdict(product="Claim Plan", price=None)
    )
    assert why is None
    assert resolved.id == product.id
    assert price == 100000


def test_resolve_terms_rejects_duplicate_names(db_session, account, context, product, owner):
    db_session.add(
        Product(
            slug="claim-plan-2", name="Claim Plan", max_price=90000, min_price=90000,
            quantity=0, merchant_id=owner.id,
        )
    )
    db_session.commit()

    resolved, price, why = claim_verification._resolve_terms(
        db_session, account, context, _verdict(product="Claim Plan", price=100000)
    )
    assert (resolved, price) == (None, None)
    assert "multiple" in why


def test_resolve_terms_ignores_drafts(db_session, account, context, product):
    product.is_draft = True
    db_session.commit()

    resolved, price, why = claim_verification._resolve_terms(
        db_session, account, context, _verdict(product="Claim Plan", price=100000)
    )
    assert (resolved, price) == (None, None)
    assert "no product matching" in why
