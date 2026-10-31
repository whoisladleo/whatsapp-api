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
