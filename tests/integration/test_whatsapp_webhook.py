import json

import httpx
import pytest
import respx

import app.services.llm as llm_module
from app.core.config import settings
from app.core.security import verify_meta_signature
from app.crud import knowledge as crud_knowledge
from app.db.models import Message, Product, User, WhatsAppAccount
from app.schemas.knowledge import KnowledgeBaseCreate

WEBHOOK_URL = "/api/v1/whatsapp/webhook"
MESSAGES_URL_RE = r"https://graph\.facebook\.com/.+/messages$"


def _text_webhook_payload(phone_number_id: str, from_phone: str, body: str, msg_id="wamid.in1"):
    return {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": msg_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }


@pytest.fixture
def merchant_owner(db_session):
    owner = User(username="255700000099")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


# --- GET /webhook verify-token handshake ------------------------------------

def test_verify_webhook_correct_token(client, monkeypatch):
    # Pinned explicitly rather than read from ambient settings, so this test
    # behaves the same whether or not a local .env happens to set this value.
    monkeypatch.setattr(settings, "WEBHOOK_VERIFY_TOKEN", "expected-verify-token")
    resp = client.get(
        f"{WEBHOOK_URL}",
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge123",
            "hub.verify_token": "expected-verify-token",
        },
    )

    assert resp.status_code == 200
    assert resp.text == "challenge123"


def test_verify_webhook_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_VERIFY_TOKEN", "expected-verify-token")
    resp = client.get(
        f"{WEBHOOK_URL}",
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge123",
            "hub.verify_token": "wrong-token",
        },
    )

    assert resp.status_code == 403


# --- POST /webhook signature verification ------------------------------------

def test_webhook_accepted_without_app_secret_configured(client, db_session):
    assert settings.WHATSAPP_APP_SECRET is None
    resp = client.post(WEBHOOK_URL, json={"entry": []})

    assert resp.status_code == 200


def test_webhook_rejects_bad_signature_when_secret_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    body = json.dumps({"entry": []}).encode()

    resp = client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )

    assert resp.status_code == 403


def test_webhook_accepts_correct_signature_when_secret_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "topsecret")
    body = json.dumps({"entry": []}).encode()
    import hashlib, hmac

    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, sig)

    resp = client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )

    assert resp.status_code == 200


# --- Routing: AI path vs merchant path ---------------------------------------

@respx.mock
def test_inbound_message_from_customer_routes_to_ai(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_AI",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out1"}]})
    )

    called_with = {}

    async def fake_generate_response(**kwargs):
        called_with.update(kwargs)
        return "Hello from Amara!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    # Replace the whole module-level `llm_service` reference (not just a
    # method on the real singleton) so this test doesn't depend on whether
    # ANTHROPIC_API_KEY happens to be set in the environment — in CI it's
    # unset, which makes the real `llm_service` None. AI processing now
    # happens inside the webhook_processing task module, not whatsapp.py.
    import app.tasks.webhook_processing as webhook_processing_module
    original = webhook_processing_module.llm_service
    webhook_processing_module.llm_service = FakeLLMService()
    try:
        payload = _text_webhook_payload("PHONE_AI", "255711111111", "Hi, I want shoes")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        webhook_processing_module.llm_service = original

    assert resp.status_code == 200
    assert called_with.get("message_body") == "Hi, I want shoes"
    assert send_route.called

    inbound = db_session.query(Message).filter(Message.direction == "inbound").first()
    assert inbound is not None
    assert inbound.body == "Hi, I want shoes"

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "Hello from Amara!"
    assert outbound.status == "sent"


@respx.mock
def test_inbound_message_from_owner_routes_to_merchant_handler(
    client, db_session, merchant_owner
):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_MERCH",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    product = Product(
        slug="shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant_owner.id,
    )
    db_session.add(product)
    db_session.commit()
    product_id = product.id

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out2"}]})
    )

    payload = _text_webhook_payload(
        "PHONE_MERCH", merchant_owner.username, "/stock Shoes +5"
    )
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert send_route.called

    # The dispatched task's `finally: db.close()` expunges `product` from
    # db_session's identity map (it shares that session in eager-mode
    # tests) — re-query rather than refresh() the now-stale instance, same
    # as the order_reminders task tests already do for the same reason.
    reloaded_product = db_session.query(Product).filter(Product.id == product_id).first()
    assert reloaded_product.quantity == 10

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "Updated stock" in outbound.body


@respx.mock
def test_merchant_routing_normalizes_phone_format(client, db_session, merchant_owner):
    """
    merchant_owner.username is "255700000099" — a message arriving with the
    same number in leading-0 form should still be recognized as the owner
    and routed to the merchant handler, not the AI path (the is_merchant
    check normalizes both sides for exactly this).
    """
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_MERCH_NORM",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    product = Product(
        slug="shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant_owner.id,
    )
    db_session.add(product)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out-norm"}]})
    )

    assert merchant_owner.username == "255700000099"
    payload = _text_webhook_payload("PHONE_MERCH_NORM", "0700000099", "/stock Shoes +5")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert send_route.called

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "Updated stock" in outbound.body


def test_inbound_message_ai_disabled_no_reply(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_NOAI",
        access_token="tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    payload = _text_webhook_payload("PHONE_NOAI", "255711111111", "Hello?")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert db_session.query(Message).filter(Message.direction == "outbound").count() == 0
    assert db_session.query(Message).filter(Message.direction == "inbound").count() == 1


def test_unknown_phone_number_id_ignored(client, db_session):
    payload = _text_webhook_payload("PHONE_DOES_NOT_EXIST", "255711111111", "Hello?")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert db_session.query(Message).count() == 0


# --- Idempotency (Meta webhook retries) --------------------------------------

@respx.mock
def test_duplicate_delivery_of_same_message_id_is_not_reprocessed(
    client, db_session, merchant_owner
):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_DEDUPE",
        access_token="tok",
        ai_enabled=True,
        callback_url="http://8.8.8.8/hook",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.dedupe-out"}]})
    )
    callback_route = respx.post("http://8.8.8.8/hook").mock(
        return_value=httpx.Response(200, json={})
    )

    called_with = []

    async def fake_generate_response(**kwargs):
        called_with.append(kwargs)
        return "Hello from Amara!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    import app.tasks.webhook_processing as webhook_processing_module
    original = webhook_processing_module.llm_service
    webhook_processing_module.llm_service = FakeLLMService()
    try:
        payload = _text_webhook_payload(
            "PHONE_DEDUPE", "255711119999", "Hi, retried delivery", msg_id="wamid.dupe1"
        )
        first_resp = client.post(WEBHOOK_URL, json=payload)
        # Meta retries the exact same delivery (same whatsapp message id).
        second_resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        webhook_processing_module.llm_service = original

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200

    # Only one inbound message persisted, AI generated exactly one reply,
    # only one outbound send happened, and the callback was only forwarded
    # once — none of the per-item processing re-ran for the retry.
    assert db_session.query(Message).filter(Message.direction == "inbound").count() == 1
    assert db_session.query(Message).filter(Message.direction == "outbound").count() == 1
    assert len(called_with) == 1
    assert send_route.call_count == 1
    assert callback_route.call_count == 1


# --- Callback forwarding + SSRF guard ----------------------------------------

@respx.mock
def test_webhook_forwards_to_safe_callback_url(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_CB_SAFE",
        access_token="tok",
        ai_enabled=False,
        callback_url="http://8.8.8.8/hook",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    callback_route = respx.post("http://8.8.8.8/hook").mock(
        return_value=httpx.Response(200, json={})
    )

    payload = _text_webhook_payload("PHONE_CB_SAFE", "255711111111", "hi")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert callback_route.called


def test_webhook_does_not_forward_to_unsafe_callback_url(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_CB_UNSAFE",
        access_token="tok",
        ai_enabled=False,
        callback_url="http://127.0.0.1/hook",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    with respx.mock(assert_all_called=False) as respx_mock:
        unsafe_route = respx_mock.post("http://127.0.0.1/hook").mock(
            return_value=httpx.Response(200, json={})
        )

        payload = _text_webhook_payload("PHONE_CB_UNSAFE", "255711111111", "hi")
        resp = client.post(WEBHOOK_URL, json=payload)

        assert resp.status_code == 200
        assert unsafe_route.called is False


def test_webhook_forwarding_swallows_request_exceptions(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_CB_ERR",
        access_token="tok",
        ai_enabled=False,
        callback_url="http://8.8.8.8/hook",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.post("http://8.8.8.8/hook").mock(side_effect=httpx.ConnectError("boom"))

        payload = _text_webhook_payload("PHONE_CB_ERR", "255711111111", "hi")
        resp = client.post(WEBHOOK_URL, json=payload)

        assert resp.status_code == 200


@respx.mock
def test_webhook_forwarding_runs_after_response_via_background_tasks(
    client, db_session, merchant_owner, monkeypatch
):
    # A slow merchant endpoint must not delay the Meta ACK: the forward is
    # scheduled via BackgroundTasks (runs after the response) instead of
    # being awaited inline. Spying add_task proves the scheduling path;
    # the actual POST still happens (the route mock below) when Starlette
    # runs the background tasks.
    import app.api.v1.whatsapp as whatsapp_module
    from fastapi import BackgroundTasks

    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_CB_BG",
        access_token="tok",
        ai_enabled=False,
        callback_url="http://8.8.8.8/hook",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    callback_route = respx.post("http://8.8.8.8/hook").mock(
        return_value=httpx.Response(200, json={})
    )

    scheduled = []
    original_add_task = BackgroundTasks.add_task

    def spy_add_task(self, func, *args, **kwargs):
        scheduled.append((func, args))
        return original_add_task(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", spy_add_task)

    payload = _text_webhook_payload("PHONE_CB_BG", "255711111111", "hi")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert callback_route.called
    assert [
        (func, args[0]) for func, args in scheduled
    ] == [(whatsapp_module._forward_to_callback, "http://8.8.8.8/hook")]


# --- Status updates, knowledge base lookup, Meta send failure ---------------


def _status_update_payload(phone_number_id, whatsapp_message_id, status):
    return {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": phone_number_id,
                            },
                            "statuses": [
                                {"id": whatsapp_message_id, "status": status}
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }


def test_status_update_webhook_updates_message_status(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_STATUS",
        access_token="tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    outbound = Message(
        whatsapp_message_id="wamid.track1",
        to_phone="255711111111",
        type="text",
        body="hi",
        direction="outbound",
        status="sent",
        account_id=account.id,
    )
    db_session.add(outbound)
    db_session.commit()

    payload = _status_update_payload("PHONE_STATUS", "wamid.track1", "delivered")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    db_session.refresh(outbound)
    assert outbound.status == "delivered"


@respx.mock
def test_ai_routing_uses_account_knowledge_base(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_KB",
        access_token="tok",
        ai_enabled=True,
        product_slug="shipping-faq",
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    crud_knowledge.create_knowledge_base(
        db_session,
        KnowledgeBaseCreate(name="Shipping", slug="shipping-faq", content="We ship in 2 days."),
        merchant_owner.id,
    )

    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out3"}]})
    )

    called_with = {}

    async def fake_generate_response(**kwargs):
        called_with.update(kwargs)
        return "We ship fast!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    import app.tasks.webhook_processing as webhook_processing_module
    original = webhook_processing_module.llm_service
    webhook_processing_module.llm_service = FakeLLMService()
    try:
        payload = _text_webhook_payload("PHONE_KB", "255711112222", "When do you ship?")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        webhook_processing_module.llm_service = original

    assert resp.status_code == 200
    assert called_with.get("knowledge_content") == "We ship in 2 days."


@respx.mock
def test_ai_reply_logged_as_failed_when_meta_send_fails(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="PHONE_FAIL",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad token"}})
    )

    async def fake_generate_response(**kwargs):
        return "Hi there!"

    class FakeLLMService:
        generate_response = staticmethod(fake_generate_response)

    import app.tasks.webhook_processing as webhook_processing_module
    original = webhook_processing_module.llm_service
    webhook_processing_module.llm_service = FakeLLMService()
    try:
        payload = _text_webhook_payload("PHONE_FAIL", "255711113333", "Hi")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        webhook_processing_module.llm_service = original

    assert resp.status_code == 200
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.status == "failed"
    assert outbound.whatsapp_message_id.startswith("failed_")


def _location_webhook_payload(phone_number_id: str, from_phone: str, msg_id="wamid.loc1"):
    return {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": msg_id,
                                    "timestamp": "1700000000",
                                    "type": "location",
                                    "location": {
                                        "latitude": -6.7924,
                                        "longitude": 39.2083,
                                        "name": "My Shop",
                                        "address": "123 Main St",
                                    },
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }


def test_inbound_location_message_dispatches_capture_task(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Loc Shop",
        phone_number_id="PHONE_LOC",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.location_capture as location_capture_module

    calls = []
    original = location_capture_module.capture_customer_location.apply_async
    location_capture_module.capture_customer_location.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _location_webhook_payload("PHONE_LOC", "255711114444")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        location_capture_module.capture_customer_location.apply_async = original

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0] == [str(account.id), "255711114444", -6.7924, 39.2083, "My Shop", "123 Main St"]

    inbound = db_session.query(Message).filter(Message.direction == "inbound").first()
    assert inbound is not None
    assert inbound.type == "location"
    assert inbound.body == "My Shop"


def test_inbound_location_message_dispatches_even_when_ai_disabled(client, db_session, merchant_owner):
    account = WhatsAppAccount(
        name="Loc Shop 2",
        phone_number_id="PHONE_LOC2",
        access_token="tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.location_capture as location_capture_module

    calls = []
    original = location_capture_module.capture_customer_location.apply_async
    location_capture_module.capture_customer_location.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _location_webhook_payload("PHONE_LOC2", "255711115555")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        location_capture_module.capture_customer_location.apply_async = original

    assert resp.status_code == 200
    assert len(calls) == 1


# --- dispatch rollback on broker failure (#78) ----------------------------------


def test_text_dispatch_failure_rolls_back_and_retry_replays(
    client, db_session, merchant_owner
):
    account = WhatsAppAccount(
        name="Rollback Shop",
        phone_number_id="PHONE_RB",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.webhook_processing as task_module

    calls = []
    original = task_module.process_inbound_message.apply_async

    def flaky(args=None, **kw):
        if not calls:
            calls.append("boom")
            raise RuntimeError("broker down")
        calls.append(args)

    task_module.process_inbound_message.apply_async = flaky
    try:
        payload = _text_webhook_payload("PHONE_RB", "255711116666", "hi", msg_id="wamid.rb1")
        with pytest.raises(RuntimeError, match="broker down"):
            client.post(WEBHOOK_URL, json=payload)
        assert (
            db_session.query(Message)
            .filter(Message.whatsapp_message_id == "wamid.rb1")
            .count()
            == 0
        )
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_message.apply_async = original

    assert resp.status_code == 200
    stored = (
        db_session.query(Message)
        .filter(Message.whatsapp_message_id == "wamid.rb1")
        .first()
    )
    assert stored is not None
    assert stored.body == "hi"
    assert calls[1] == [str(account.id), "255711116666", "hi"]


def test_location_dispatch_failure_rolls_back_and_retry_replays(
    client, db_session, merchant_owner
):
    account = WhatsAppAccount(
        name="Rollback Loc Shop",
        phone_number_id="PHONE_RB_LOC",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.location_capture as location_capture_module

    calls = []
    original = location_capture_module.capture_customer_location.apply_async

    def flaky(args=None, **kw):
        if not calls:
            calls.append("boom")
            raise RuntimeError("broker down")
        calls.append(args)

    location_capture_module.capture_customer_location.apply_async = flaky
    try:
        payload = _location_webhook_payload("PHONE_RB_LOC", "255711117777", msg_id="wamid.rbloc1")
        with pytest.raises(RuntimeError, match="broker down"):
            client.post(WEBHOOK_URL, json=payload)
        assert (
            db_session.query(Message)
            .filter(Message.whatsapp_message_id == "wamid.rbloc1")
            .count()
            == 0
        )
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        location_capture_module.capture_customer_location.apply_async = original

    assert resp.status_code == 200
    stored = (
        db_session.query(Message)
        .filter(Message.whatsapp_message_id == "wamid.rbloc1")
        .first()
    )
    assert stored is not None
    assert stored.type == "location"
    assert calls[1][0] == str(account.id)
    assert calls[1][1] == "255711117777"


def test_unsigned_webhook_rejected_when_secret_unset_and_flag_off(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", None)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)

    resp = client.post(
        WEBHOOK_URL, json=_text_webhook_payload("PHONE_X", "255700000000", "hi")
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Webhook signature verification is not configured"


def _button_tap_payload(phone_number_id, from_phone, button_id, context_id=None, msg_id="wamid.tap1"):
    message = {
        "from": from_phone,
        "id": msg_id,
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": button_id, "title": "Reply"},
        },
    }
    if context_id:
        message["context"] = {"id": context_id}
    return {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [message],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }


@respx.mock
def test_handoff_reply_tap_arms_reply_mode_and_confirms(
    client, db_session, merchant_owner, monkeypatch
):
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())

    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PHONE_PLATFORM",
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    business = WhatsAppAccount(
        name="Shop",
        phone_number_id="PHONE_TAP",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(business)
    db_session.commit()

    from app.db.models import ConversationContext, ConversationState, Customer

    customer = Customer(phone_number="255711111111")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    )
    db_session.add(
        Message(
            whatsapp_message_id="wamid.histtap",
            from_phone="255711111111",
            type="text",
            body="hello?",
            direction="inbound",
            status="delivered",
            account_id=business.id,
        )
    )
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.confirm1"}]})
    )

    owner_id = merchant_owner.id
    payload = _button_tap_payload("PHONE_PLATFORM", merchant_owner.username, "handoff_reply")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert send_route.called
    assert flow_module.get_reply_mode(owner_id)["ref"] == "255711111111"

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "You're replying to 255711111111" in outbound.body


@respx.mock
def test_unknown_platform_tap_answers_loudly_instead_of_silence(
    client, db_session, merchant_owner
):
    # A tap Meta drifted (unknown id/title) on the platform chat used to
    # die at the gate with zero feedback; now the merchant gets a hint
    # pointing at /reply (#96).
    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PHONE_PLATFORM2",
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})
    )

    message = {
        "from": merchant_owner.username,
        "id": "wamid.tap1",
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "some_other_flow", "title": "Something else"},
        },
    }
    payload = {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": "PHONE_PLATFORM2",
                            },
                            "messages": [message],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert send_route.called
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "That tap didn't come through" in outbound.body


@respx.mock
def test_unknown_business_number_tap_still_dropped(
    client, db_session, merchant_owner
):
    # Loud fallback is platform-chat-only: a random tap on a business
    # number keeps the old stored-but-silent behavior.
    business = WhatsAppAccount(
        name="Shop",
        phone_number_id="PHONE_BIZTAP",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(business)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.y"}]})
    )

    message = {
        "from": merchant_owner.username,
        "id": "wamid.tap9",
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "some_other_flow", "title": "Something else"},
        },
    }
    payload = {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": "PHONE_BIZTAP",
                            },
                            "messages": [message],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert not send_route.called
    stored = db_session.query(Message).filter(Message.whatsapp_message_id == "wamid.tap9").first()
    assert stored is not None


@respx.mock
def test_handoff_tap_routing_accepts_title_as_id(client, db_session, merchant_owner, monkeypatch):
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())

    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PHONE_PLATFORM3",
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    db_session.commit()

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.confirm2"}]})
    )

    # Meta may echo the button title as the tap id instead of a payload.
    message = {
        "from": merchant_owner.username,
        "id": "wamid.tap3",
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "✅ Done", "title": "✅ Done"},
        },
    }
    payload = {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": "PHONE_PLATFORM3",
                            },
                            "messages": [message],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert send_route.called
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "Nothing in progress" in outbound.body


def _button_type_tap_payload(phone_number_id, from_phone, text, msg_id):
    # Template quick-reply taps may arrive as type "button" (no payload
    # id — the title IS the payload) instead of type "interactive".
    return {
        "entry": [
            {
                "id": "entry1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "255700000000",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": msg_id,
                                    "timestamp": "1700000000",
                                    "type": "button",
                                    "button": {"text": text, "payload": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ]
    }


def _platform_plus_handoff(db_session, merchant_owner, platform_phone_id):
    from app.db.models import ConversationContext, ConversationState, Customer

    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id=platform_phone_id,
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    business = WhatsAppAccount(
        name="Shop",
        phone_number_id=f"BIZ_{platform_phone_id}",
        access_token="tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(business)
    db_session.commit()

    customer = Customer(phone_number="255711111111")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    db_session.add(
        ConversationContext(customer_id=customer.id, state=ConversationState.HUMAN_HANDOFF)
    )
    db_session.add(
        Message(
            whatsapp_message_id=f"wamid.hist_{platform_phone_id}",
            from_phone="255711111111",
            type="text",
            body="hello?",
            direction="inbound",
            status="delivered",
            account_id=business.id,
        )
    )
    db_session.commit()
    return customer


@respx.mock
def test_button_type_reply_tap_arms_and_next_text_relays(
    client, db_session, merchant_owner, monkeypatch
):
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())
    _platform_plus_handoff(db_session, merchant_owner, "PHONE_BTN1")
    # The eager task closes the request session, detaching merchant_owner
    # — capture plain values before posting, as the tap test above does.
    owner_id = merchant_owner.id
    owner_username = merchant_owner.username

    # One distinct id per send (confirm, relay, sent-receipt):
    # create_message dedups on whatsapp_message_id, so a single canned
    # id would collapse all three outbound rows into one.
    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        side_effect=[
            httpx.Response(200, json={"messages": [{"id": "wamid.btnconfirm"}]}),
            httpx.Response(200, json={"messages": [{"id": "wamid.btnrelay"}]}),
            httpx.Response(200, json={"messages": [{"id": "wamid.btnsent"}]}),
        ]
    )

    tap = client.post(
        WEBHOOK_URL,
        json=_button_type_tap_payload(
            "PHONE_BTN1", owner_username, "Reply", "wamid.btntap1"
        ),
    )
    assert tap.status_code == 200
    assert send_route.called
    assert flow_module.get_reply_mode(owner_id)["ref"] == "255711111111"
    confirm = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "You're replying to 255711111111" in confirm.body

    # The merchant's next plain-text message on the platform chat relays
    # to the customer instead of hitting "Unknown command".
    text = client.post(
        WEBHOOK_URL,
        json=_text_webhook_payload(
            "PHONE_BTN1", owner_username, "Yes, still available!", msg_id="wamid.btntxt1"
        ),
    )
    assert text.status_code == 200
    assert flow_module.get_reply_mode(owner_id) is None

    bodies = [json.loads(call.request.content) for call in send_route.calls]
    assert any(
        call.get("to") == "255711111111"
        and call.get("text", {}).get("body") == "Yes, still available!"
        for call in bodies
    )
    merchant_replies = db_session.query(Message).filter(
        Message.direction == "outbound", Message.to_phone == owner_username
    ).all()
    assert any("Sent to 255711111111." in m.body for m in merchant_replies)


@respx.mock
def test_button_type_takeover_tap_resumes_and_confirms(
    client, db_session, merchant_owner, monkeypatch
):
    import fakeredis

    import app.services.handoff_flow as flow_module
    from app.db.models import ConversationContext, ConversationState, Customer

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())
    # The take-over turn is covered in the resume tests; running it nested
    # and eager here would corrupt this test's session, so suppress just
    # the dispatch — the verdict and reply text stay real.
    verdicts = []
    monkeypatch.setattr(
        "app.services.resume.dispatch_resume_reply",
        lambda verdict: verdicts.append(verdict) or False,
    )
    _platform_plus_handoff(db_session, merchant_owner, "PHONE_BTN2")
    owner_username = merchant_owner.username

    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.btn2"}]})
    )

    tap = client.post(
        WEBHOOK_URL,
        json=_button_type_tap_payload(
            "PHONE_BTN2", owner_username, "Amara, take over.", "wamid.btntap2"
        ),
    )
    assert tap.status_code == 200
    assert send_route.called

    context = (
        db_session.query(ConversationContext)
        .join(Customer, ConversationContext.customer_id == Customer.id)
        .filter(Customer.phone_number == "255711111111")
        .first()
    )
    assert context.state == ConversationState.NEGOTIATING
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "Amara is back on 255711111111" in outbound.body
    assert "replying to their last message now" in outbound.body
    assert [v.answerable for v in verdicts] == [True]


@respx.mock
def test_platform_yes_reply_fires_subscription_push(
    client, db_session, merchant_owner, monkeypatch
):
    # End-to-end wiring: a merchant answering YES on the platform chat
    # reuses/creates their pending subscription order and fires the Tunzaa
    # prompt — no dashboard login needed.
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())

    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PHONE_YES",
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    db_session.add(
        Product(
            slug="taskflow-os-subscription", name="Taskflow OS Subscription",
            max_price=100000, min_price=100000, quantity=999999,
            merchant_id=merchant_owner.id, is_subscription=True,
            is_platform_plan=True, monthly_reply_limit=2000,
        )
    )
    db_session.commit()

    respx.post("https://pay.tunzaa.co.tz/accounts/request/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    pay_route = respx.post("https://pay.tunzaa.co.tz/payments/initiate-payment").mock(
        return_value=httpx.Response(
            200, json={"statusCode": 202, "success": True, "transactionID": "TXN_YES"}
        )
    )
    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.yesreply"}]})
    )

    payload = _text_webhook_payload("PHONE_YES", merchant_owner.username, "YES", msg_id="wamid.yesm1")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert pay_route.call_count == 1
    assert send_route.called
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "sent to 255700000099" in outbound.body


def _yes_tap_payload(phone_number_id, from_phone, button_id, title, msg_id="wamid.yestap1"):
    payload = _button_tap_payload(phone_number_id, from_phone, button_id, msg_id=msg_id)
    reply = payload["entry"][0]["changes"][0]["value"]["messages"][0]["interactive"]["button_reply"]
    reply["title"] = title
    return payload


def _platform_with_plan(db_session, merchant_owner, phone_number_id="PHONE_YES"):
    platform = WhatsAppAccount(
        name="Taskflow",
        phone_number_id=phone_number_id,
        access_token="tok",
        ai_enabled=True,
        is_platform_account=True,
        user_id=merchant_owner.id,
    )
    db_session.add(platform)
    db_session.add(
        Product(
            slug="taskflow-os-subscription", name="Taskflow OS Subscription",
            max_price=100000, min_price=100000, quantity=999999,
            merchant_id=merchant_owner.id, is_subscription=True,
            is_platform_plan=True, monthly_reply_limit=2000,
        )
    )
    db_session.commit()
    return platform


def _mock_tunzaa_push(txn_id="TXN_YES"):
    respx.post("https://pay.tunzaa.co.tz/accounts/request/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    return respx.post("https://pay.tunzaa.co.tz/payments/initiate-payment").mock(
        return_value=httpx.Response(
            200, json={"statusCode": 202, "success": True, "transactionID": txn_id}
        )
    )


@respx.mock
def test_platform_yes_button_id_fires_subscription_push(
    client, db_session, merchant_owner, monkeypatch
):
    # The nudge's quick-reply button: tapping it pays like typing YES.
    # This covers the tap-id path (title intentionally unrelated).
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())
    _platform_with_plan(db_session, merchant_owner)
    pay_route = _mock_tunzaa_push()
    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.yesreply"}]})
    )

    payload = _yes_tap_payload("PHONE_YES", merchant_owner.username, "Yes", "Pay now")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert pay_route.call_count == 1
    assert send_route.called
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert "sent to 255700000099" in outbound.body


@respx.mock
def test_platform_yes_button_title_fires_subscription_push(
    client, db_session, merchant_owner, monkeypatch
):
    # Same tap, but Meta drifted the id — the title still matches.
    import fakeredis

    import app.services.handoff_flow as flow_module

    monkeypatch.setattr(flow_module, "redis_client", fakeredis.FakeRedis())
    _platform_with_plan(db_session, merchant_owner, phone_number_id="PHONE_YES2")
    pay_route = _mock_tunzaa_push()
    send_route = respx.post(url__regex=MESSAGES_URL_RE).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.yesreply"}]})
    )

    payload = _yes_tap_payload(
        "PHONE_YES2", merchant_owner.username, "nudge_yes_drifted", "Yes"
    )
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert pay_route.call_count == 1
    assert send_route.called
