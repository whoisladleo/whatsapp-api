from datetime import datetime, timedelta

import fakeredis
import pytest
import respx
import httpx

import app.services.handoff_flow as flow_module
from app.services.handoff_flow import (
    BUTTON_DONE,
    BUTTON_REPLY,
    BUTTON_TAKEOVER,
    arm_reply_mode,
    clear_reply_mode,
    consume_reply_mode,
    get_reply_mode,
    handle_button,
    list_pending_handoffs,
    lookup_alert,
    remember_alert,
    resolve_merchant,
    target_still_pending,
)
from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Message,
    User,
    WhatsAppAccount,
)

WHATSAPP_MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"
INSTAGRAM_MESSAGES_URL_RE = r"https://graph\.instagram\.com/.+/messages$"


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())


@pytest.fixture
def merchant(db_session):
    user = User(username="255700000099")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def business_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Shop",
        phone_number_id="PHONE_FLOW1",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def wa_handoff(db_session, business_account):
    """A WhatsApp customer mid-handoff with real message history."""
    customer = Customer(phone_number="255711111111")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    context = ConversationContext(
        customer_id=customer.id,
        state=ConversationState.HUMAN_HANDOFF,
        handoff_at=datetime.utcnow() - timedelta(minutes=5),
    )
    db_session.add(context)
    db_session.add(
        Message(
            whatsapp_message_id="wamid.hist1",
            from_phone="255711111111",
            type="text",
            body="hello?",
            direction="inbound",
            status="delivered",
            account_id=business_account.id,
        )
    )
    db_session.commit()
    return customer


@pytest.fixture
def ig_handoff(db_session, merchant):
    account = InstagramAccount(
        ig_business_id="ig_biz_flow",
        access_token="ig-tok",
        username="shop",
        ai_enabled=True,
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    customer = InstagramCustomer(
        ig_scoped_id="igsid_flow", username="waitingbuyer", account_id=account.id
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        InstagramConversationContext(
            customer_id=customer.id,
            state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow() - timedelta(minutes=2),
        )
    )
    db_session.commit()
    return customer


def _target(channel="whatsapp", ref="255711111111", **overrides):
    target = {
        "channel": channel,
        "ref": ref,
        "name": ref if channel == "whatsapp" else f"@{ref}",
        "account_id": None,
        "customer_id": None,
    }
    target.update(overrides)
    return target


# --- merchant resolution + alert mapping ------------------------------------


def test_resolve_merchant_matches_owner_phone(db_session, merchant):
    assert resolve_merchant(db_session, "255700000099").id == merchant.id
    assert resolve_merchant(db_session, "255700000000") is None


def test_alert_mapping_round_trip():
    target = _target()
    remember_alert("wamid.alert1", target)
    assert lookup_alert("wamid.alert1") == target
    assert lookup_alert("wamid.unknown") is None


# --- pending listing ---------------------------------------------------------


def test_list_pending_handoffs_covers_both_channels_most_recent_first(
    db_session, merchant, business_account, wa_handoff, ig_handoff
):
    pending = list_pending_handoffs(db_session, merchant)

    assert [p["channel"] for p in pending] == ["instagram", "whatsapp"]
    assert pending[0]["ref"] == "waitingbuyer"
    assert pending[0]["name"] == "@waitingbuyer"
    assert pending[1]["ref"] == "255711111111"
    assert pending[1]["account_id"] == str(business_account.id)


def test_list_pending_handoffs_ignores_non_handoff_and_foreign_history(
    db_session, merchant, business_account
):
    # Negotiating conversation: not pending.
    customer = Customer(phone_number="255722222222")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        ConversationContext(customer_id=customer.id, state=ConversationState.NEGOTIATING)
    )
    # Handoff conversation with no history on this merchant's accounts: not theirs.
    stranger = Customer(phone_number="255733333333")
    db_session.add(stranger)
    db_session.commit()
    db_session.refresh(stranger)
    db_session.add(
        ConversationContext(
            customer_id=stranger.id,
            state=ConversationState.HUMAN_HANDOFF,
            handoff_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    assert list_pending_handoffs(db_session, merchant) == []


def test_target_still_pending_flips_after_resume(
    db_session, merchant, business_account, wa_handoff
):
    from app.crud import message as crud_message

    target = _target(account_id=str(business_account.id))
    assert target_still_pending(db_session, target) is True

    crud_message.resume_conversation_for_account(db_session, business_account.id, "255711111111")

    assert target_still_pending(db_session, target) is False


# --- reply-mode state --------------------------------------------------------


def test_reply_mode_arm_get_clear(merchant):
    assert get_reply_mode(merchant.id) is None

    assert arm_reply_mode(merchant.id, _target()) is True
    assert get_reply_mode(merchant.id) == _target()

    clear_reply_mode(merchant.id)
    assert get_reply_mode(merchant.id) is None


# --- button handling ---------------------------------------------------------


async def test_reply_button_with_no_pending(db_session, merchant):
    assert await handle_button(db_session, merchant, BUTTON_REPLY, None) == (
        "No customers are waiting for you right now."
    )


async def test_reply_button_unknown_sender_stays_silent(db_session, merchant):
    assert await handle_button(db_session, None, BUTTON_REPLY, None) is None


async def test_unknown_button_from_merchant_answers_loudly(db_session, merchant):
    # A resolved merchant tapping something unrecognized is never a
    # stranger — silence would strand them with no relay and no hint (#96).
    reply = await handle_button(db_session, merchant, "not_a_handoff_button", None)

    assert reply == "That tap didn't come through — reply with /reply instead."


def test_resolve_merchant_matches_pre_normalization_username(db_session):
    user = User(username="0700000099")
    db_session.add(user)
    db_session.commit()

    assert resolve_merchant(db_session, "255700000099").id == user.id
    assert resolve_merchant(db_session, "255799999999") is None


def test_normalize_button_id_matches_titles_case_insensitively():
    from app.services.handoff_flow import BUTTON_REPLY, normalize_button_id

    assert normalize_button_id("0", "reply") == BUTTON_REPLY
    assert normalize_button_id("  REPLY  ", None) == BUTTON_REPLY


async def test_reply_button_resolves_exact_alert_via_context(
    db_session, merchant, business_account, wa_handoff, ig_handoff
):
    # The tap names the OLDER (WhatsApp) alert — context wins over recency.
    remember_alert("wamid.alert_old", _target(account_id=str(business_account.id)))

    reply = await handle_button(db_session, merchant, BUTTON_REPLY, "wamid.alert_old")

    assert reply == "You're replying to 255711111111 — send your message here, or /cancel."
    assert get_reply_mode(merchant.id)["ref"] == "255711111111"


async def test_reply_button_falls_back_to_most_recent_without_context(
    db_session, merchant, business_account, wa_handoff, ig_handoff
):
    reply = await handle_button(db_session, merchant, BUTTON_REPLY, None)

    assert "You're replying to @waitingbuyer" in reply
    assert "most recent of 2 waiting" in reply
    assert get_reply_mode(merchant.id)["channel"] == "instagram"


async def test_reply_button_on_stale_alert_names_it(
    db_session, merchant, business_account, wa_handoff
):
    from app.crud import message as crud_message

    remember_alert("wamid.alert_stale", _target(account_id=str(business_account.id)))
    crud_message.resume_conversation_for_account(db_session, business_account.id, "255711111111")

    reply = await handle_button(db_session, merchant, BUTTON_REPLY, "wamid.alert_stale")

    assert "no longer waiting" in reply
    assert get_reply_mode(merchant.id) is None


async def test_done_button_disarms_and_reports(db_session, merchant, business_account, wa_handoff):
    arm_reply_mode(merchant.id, _target())

    reply = await handle_button(db_session, merchant, BUTTON_DONE, None)

    assert "stays paused" in reply
    assert get_reply_mode(merchant.id) is None


async def test_takeover_button_resumes_instagram(
    db_session, merchant, business_account, wa_handoff, ig_handoff
):
    reply = await handle_button(db_session, merchant, BUTTON_TAKEOVER, None)

    assert "Amara is back on @waitingbuyer" in reply
    context = (
        db_session.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == ig_handoff.id)
        .first()
    )
    assert context.state == ConversationState.NEGOTIATING


async def test_takeover_button_resumes_whatsapp_via_context(
    db_session, merchant, business_account, wa_handoff, ig_handoff
):
    remember_alert("wamid.alert_wa", _target(account_id=str(business_account.id)))

    reply = await handle_button(db_session, merchant, BUTTON_TAKEOVER, "wamid.alert_wa")

    assert "Amara is back on 255711111111" in reply
    context = (
        db_session.query(ConversationContext)
        .join(Customer, ConversationContext.customer_id == Customer.id)
        .filter(Customer.phone_number == "255711111111")
        .first()
    )
    assert context.state == ConversationState.NEGOTIATING


# --- reply-mode consume ------------------------------------------------------


async def test_consume_returns_none_when_not_armed(db_session, merchant):
    assert await consume_reply_mode(db_session, merchant, "hello?") is None


async def test_consume_cancel_disarms(db_session, merchant):
    arm_reply_mode(merchant.id, _target())

    assert await consume_reply_mode(db_session, merchant, "/cancel") == "Cancelled — nothing was sent."
    assert get_reply_mode(merchant.id) is None


@respx.mock
async def test_consume_relays_to_whatsapp_and_disarms(
    db_session, merchant, business_account, wa_handoff
):
    send_route = respx.post(url__regex=WHATSAPP_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.rel1"}]})
    )
    arm_reply_mode(merchant.id, _target(account_id=str(business_account.id)))

    reply = await consume_reply_mode(db_session, merchant, "Yes, still available!")

    assert reply == "Sent to 255711111111."
    assert send_route.called
    assert get_reply_mode(merchant.id) is None
    outbound = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.to_phone == "255711111111")
        .first()
    )
    assert outbound.body == "Yes, still available!"


@respx.mock
async def test_consume_relays_to_instagram_and_disarms(db_session, merchant, ig_handoff):
    send_route = respx.post(url__regex=INSTAGRAM_MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"message_id": "mid.rel1"})
    )
    arm_reply_mode(
        merchant.id,
        _target(channel="instagram", ref="waitingbuyer", customer_id=str(ig_handoff.id)),
    )

    reply = await consume_reply_mode(db_session, merchant, "On my way!")

    assert reply == "Sent to @waitingbuyer."
    assert send_route.called
    assert get_reply_mode(merchant.id) is None


@respx.mock
async def test_consume_send_failure_stays_armed(
    db_session, merchant, business_account, wa_handoff, monkeypatch
):
    import app.services.handoff_flow as flow_module

    async def boom(*args, **kwargs):
        raise RuntimeError("graph is down")

    monkeypatch.setattr(
        "app.core.provider.WhatsAppProvider.send_text", boom
    )
    arm_reply_mode(merchant.id, _target(account_id=str(business_account.id)))

    reply = await consume_reply_mode(db_session, merchant, "hello?")

    assert "Failed to send" in reply
    assert "still armed" in reply
    assert get_reply_mode(merchant.id) is not None


def test_normalize_button_id_accepts_payload_or_title():
    from app.services.handoff_flow import normalize_button_id

    assert normalize_button_id("handoff_reply", "Reply") == "handoff_reply"
    assert normalize_button_id("Reply", "Reply") == "handoff_reply"
    assert normalize_button_id("✏️ Reply", "✏️ Reply") == "handoff_reply"
    assert normalize_button_id("weird-id", "Done") == "handoff_done"
    assert normalize_button_id("Amara, take over.", None) == "handoff_takeover"
    assert normalize_button_id("Amara, take over", None) == "handoff_takeover"
    assert normalize_button_id("other", "Other") == "other"
    assert normalize_button_id(None, None) is None
