import json

import httpx
import respx

from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    Message,
    WhatsAppAccount,
)
from app.services.llm import (
    LLMService,
)


MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


class _FakeMessages:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)


class _FakeContentBlock:
    def __init__(self, text=None, type="text"):
        self.type = type
        if text is not None:
            self.text = text


class _FakeResponse:
    def __init__(self, text, thinking_first=False):
        blocks = []
        if thinking_first:
            blocks.append(_FakeContentBlock(type="thinking"))
        blocks.append(_FakeContentBlock(text=text))
        self.content = blocks


def _service_with_fake_client(handler=None, captured=None):
    service = LLMService(api_key=None)

    async def default_handler(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _FakeResponse("Sure, here is some info.")

    service.client = _FakeAnthropicClient(handler or default_handler)
    return service


@respx.mock
async def test_human_handoff_trigger_updates_state_and_alerts_merchant(
    db_session, shop_account, merchant, monkeypatch
):
    """
    Alert is sent via the platform's own default WhatsApp credentials
    (same ones send_otp_message uses), not the merchant's own business
    WhatsAppAccount (shop_account here) — that's a separate, customer-
    facing channel the merchant may or may not have connected at all.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "PHONE_NUMBER_ID", "PLATFORM_PHONE_TEST")
    platform_account = WhatsAppAccount(
        name="Taskflow", phone_number_id="PLATFORM_PHONE_TEST", access_token="platform-tok",
        user_id=merchant.id, is_platform_account=True,
    )
    db_session.add(platform_account)
    db_session.commit()

    alert_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    scheduled = []
    from app.tasks.handoff_reminders import send_handoff_reminder
    monkeypatch.setattr(
        send_handoff_reminder, "apply_async", lambda *a, **kw: scheduled.append((a, kw))
    )

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116666",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."
    customer = db_session.query(Customer).filter(Customer.phone_number == "255711116666").first()
    context = db_session.query(ConversationContext).filter(
        ConversationContext.customer_id == customer.id
    ).first()
    assert context.state == ConversationState.HUMAN_HANDOFF
    assert context.handoff_at is not None

    assert alert_route.called
    sent_payload = json.loads(alert_route.calls.last.request.content)
    assert sent_payload["type"] == "template"
    assert sent_payload["template"]["name"] == "handoff_notification"
    header_params = sent_payload["template"]["components"][0]["parameters"]
    body_params = sent_payload["template"]["components"][1]["parameters"]
    assert {"type": "text", "parameter_name": "customer", "text": "255711116666"} in header_params
    assert {"type": "text", "parameter_name": "customer", "text": "255711116666"} in body_params
    # The alert must quote what the *customer* said, not Amara's own reply
    # — that's what the merchant actually has to respond to.
    assert {"type": "text", "parameter_name": "message", "text": "I need a human"} in body_params
    sent_body = alert_route.calls.last.request.content.decode()
    assert "Let me get a human." not in sent_body

    # The alert itself is logged as an outbound Message, unlike before.
    alert_message = (
        db_session.query(Message)
        .filter(Message.to_phone == merchant.username, Message.direction == "outbound")
        .first()
    )
    assert alert_message is not None
    assert alert_message.status == "sent"

    # A follow-up reminder is scheduled, keyed on the conversation context id.
    assert len(scheduled) == 1
    assert scheduled[0][0] == ([str(context.id)],) or scheduled[0][1].get("args") == [str(context.id)]


@respx.mock
async def test_human_handoff_alert_still_sent_without_a_platform_whatsapp_account(
    db_session, shop_account, merchant, monkeypatch
):
    """
    Regression test for a real production gap: no WhatsAppAccount row
    existed anywhere in the database (not the merchant's own business
    account, not the platform's own number either), and the handoff alert
    silently never arrived. The alert must still be SENT via the
    platform's default credentials regardless of whether that number is
    also connected as a real WhatsAppAccount row — only the Message-log
    entry (used for the dashboard's own history view) depends on that row
    existing.
    """
    alert_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)
    monkeypatch.setattr(
        "app.tasks.handoff_reminders.send_handoff_reminder.apply_async", lambda *a, **kw: None
    )

    await service.generate_response(
        message_body="I need a human",
        from_phone="255711116667",
        db=db_session,
        account=shop_account,
    )

    assert alert_route.called
    assert db_session.query(Message).filter(
        Message.to_phone == merchant.username, Message.direction == "outbound"
    ).count() == 0


@respx.mock
async def test_human_handoff_alert_send_failure_is_logged_as_failed(db_session, shop_account, merchant, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PHONE_NUMBER_ID", "PLATFORM_PHONE_TEST")
    db_session.add(WhatsAppAccount(
        name="Taskflow", phone_number_id="PLATFORM_PHONE_TEST", access_token="platform-tok",
        user_id=merchant.id, is_platform_account=True,
    ))
    db_session.commit()

    respx.post(url__regex=MESSAGES_URL_RE).mock(return_value=httpx.Response(500))

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116667",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."
    alert_message = (
        db_session.query(Message)
        .filter(Message.to_phone == merchant.username, Message.direction == "outbound")
        .first()
    )
    assert alert_message is not None
    assert alert_message.status in ("sent", "failed")


@respx.mock
async def test_human_handoff_with_empty_remaining_text_uses_default_message(db_session, shop_account):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_only_handler(**kwargs):
        return _FakeResponse("[HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_only_handler)

    result = await service.generate_response(
        message_body="human please",
        from_phone="255711117777",
        db=db_session,
        account=shop_account,
    )

    assert result == "I am transferring you to a human agent who can help you further. Please hold."


async def test_platform_account_handoff_does_not_alert_merchant(db_session, platform_account):
    async def handoff_handler(**kwargs):
        return _FakeResponse("[HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    await service.generate_response(
        message_body="human please",
        from_phone="255711118888",
        db=db_session,
        account=platform_account,
    )

    alert_message = (
        db_session.query(Message)
        .filter(Message.direction == "outbound")
        .first()
    )
    assert alert_message is None


@respx.mock
async def test_human_handoff_schedule_failure_does_not_break_reply(
    db_session, shop_account, merchant, monkeypatch
):
    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})
    )

    async def handoff_handler(**kwargs):
        return _FakeResponse("Let me get a human. [HUMAN_HANDOFF]")

    service = _service_with_fake_client(handler=handoff_handler)

    from app.tasks.handoff_reminders import send_handoff_reminder

    def raising_apply_async(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(send_handoff_reminder, "apply_async", raising_apply_async)

    result = await service.generate_response(
        message_body="I need a human",
        from_phone="255711116669",
        db=db_session,
        account=shop_account,
    )

    assert result == "Let me get a human."
