import hashlib
import hmac
import json

import pytest

from app.core.config import settings
from app.core.security import verify_meta_signature
from app.db.models import (
    InstagramAccount,
    InstagramComment,
    InstagramCustomer,
    InstagramDirectMessage,
    User,
)

WEBHOOK_URL = "/api/v1/instagram/webhook"


def _comment_payload(ig_business_id: str, comment_id: str, text: str = "how much?"):
    return {
        "entry": [
            {
                "id": ig_business_id,
                "changes": [
                    {
                        "value": {
                            "from": {"id": "igsid_1", "username": "commenter1"},
                            "media": {"id": "media_1", "media_product_type": "REELS"},
                            "id": comment_id,
                            "text": text,
                        },
                        "field": "comments",
                    }
                ],
            }
        ]
    }


def _dm_payload(ig_business_id: str, ig_message_id: str, text: str = "is it still available?"):
    return {
        "entry": [
            {
                "id": ig_business_id,
                "messaging": [
                    {
                        "sender": {"id": "igsid_dm_1"},
                        "recipient": {"id": ig_business_id},
                        "message": {"mid": ig_message_id, "text": text},
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
    monkeypatch.setattr(settings, "INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "expected-verify-token")
    resp = client.get(
        WEBHOOK_URL,
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge123",
            "hub.verify_token": "expected-verify-token",
        },
    )

    assert resp.status_code == 200
    assert resp.text == "challenge123"


def test_verify_webhook_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "expected-verify-token")
    resp = client.get(
        WEBHOOK_URL,
        params={
            "hub.mode": "subscribe",
            "hub.challenge": "challenge123",
            "hub.verify_token": "wrong-token",
        },
    )

    assert resp.status_code == 403


# --- POST /webhook signature verification ------------------------------------

def test_webhook_accepted_without_app_secret_configured(client):
    assert settings.INSTAGRAM_APP_SECRET is None
    resp = client.post(WEBHOOK_URL, json={"entry": []})

    assert resp.status_code == 200


def test_webhook_rejects_bad_signature_when_secret_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "topsecret")
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
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "topsecret")
    body = json.dumps({"entry": []}).encode()

    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, sig, app_secret="topsecret")

    resp = client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )

    assert resp.status_code == 200


# --- Account resolution + dispatch --------------------------------------------

def test_unknown_ig_business_id_ignored(client, db_session):
    payload = _comment_payload("ig_does_not_exist", "comment_1")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert db_session.query(InstagramComment).count() == 0


def test_known_account_ai_enabled_stores_comment_and_dispatches_task(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_1",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.instagram_comment_processing as task_module

    calls = []
    original = task_module.process_inbound_comment.apply_async
    task_module.process_inbound_comment.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _comment_payload("ig_business_1", "comment_1")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_comment.apply_async = original

    assert resp.status_code == 200

    comment = db_session.query(InstagramComment).filter(
        InstagramComment.comment_id == "comment_1"
    ).first()
    assert comment is not None
    assert comment.account_id == account.id
    assert comment.body == "how much?"
    assert comment.from_username == "commenter1"
    assert comment.from_ig_scoped_id == "igsid_1"
    assert comment.media_id == "media_1"

    assert len(calls) == 1
    assert calls[0] == [str(account.id), str(comment.id)]


def test_known_account_ai_disabled_stores_comment_without_dispatch(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_2",
        access_token="ig-tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.tasks.instagram_comment_processing as task_module

    calls = []
    original = task_module.process_inbound_comment.apply_async
    task_module.process_inbound_comment.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _comment_payload("ig_business_2", "comment_2")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_comment.apply_async = original

    assert resp.status_code == 200
    assert db_session.query(InstagramComment).filter(
        InstagramComment.comment_id == "comment_2"
    ).count() == 1
    assert len(calls) == 0


# --- Idempotency (Meta webhook retries) --------------------------------------

def test_duplicate_delivery_of_same_comment_id_is_not_reprocessed(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_3",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.tasks.instagram_comment_processing as task_module

    calls = []
    original = task_module.process_inbound_comment.apply_async
    task_module.process_inbound_comment.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _comment_payload("ig_business_3", "comment_dupe")
        first_resp = client.post(WEBHOOK_URL, json=payload)
        second_resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_comment.apply_async = original

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert db_session.query(InstagramComment).filter(
        InstagramComment.comment_id == "comment_dupe"
    ).count() == 1
    assert len(calls) == 1


# --- DM account resolution + dispatch -----------------------------------------

def test_unknown_ig_business_id_ignored_for_dm(client, db_session):
    payload = _dm_payload("ig_does_not_exist", "mid_1")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    assert db_session.query(InstagramDirectMessage).count() == 0


def test_known_account_ai_enabled_stores_dm_and_dispatches_task(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_1",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.tasks.instagram_dm_processing as task_module

    calls = []
    original = task_module.process_inbound_dm.apply_async
    task_module.process_inbound_dm.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _dm_payload("ig_business_dm_1", "mid_1")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_dm.apply_async = original

    assert resp.status_code == 200

    dm = db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.ig_message_id == "mid_1"
    ).first()
    assert dm is not None
    assert dm.account_id == account.id
    assert dm.direction == "inbound"
    assert dm.body == "is it still available?"

    customer = db_session.query(InstagramCustomer).filter(
        InstagramCustomer.ig_scoped_id == "igsid_dm_1"
    ).first()
    assert customer is not None
    assert dm.customer_id == customer.id

    assert len(calls) == 1
    assert calls[0] == [str(account.id), str(customer.id), str(dm.id)]


def test_known_account_ai_disabled_stores_dm_without_dispatch(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_2",
        access_token="ig-tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.tasks.instagram_dm_processing as task_module

    calls = []
    original = task_module.process_inbound_dm.apply_async
    task_module.process_inbound_dm.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _dm_payload("ig_business_dm_2", "mid_2")
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_dm.apply_async = original

    assert resp.status_code == 200
    assert db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.ig_message_id == "mid_2"
    ).count() == 1
    assert len(calls) == 0


def test_duplicate_delivery_of_same_dm_id_is_not_reprocessed(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_3",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.tasks.instagram_dm_processing as task_module

    calls = []
    original = task_module.process_inbound_dm.apply_async
    task_module.process_inbound_dm.apply_async = lambda args=None, **kw: calls.append(args)
    try:
        payload = _dm_payload("ig_business_dm_3", "mid_dupe")
        first_resp = client.post(WEBHOOK_URL, json=payload)
        second_resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_dm.apply_async = original

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.ig_message_id == "mid_dupe"
    ).count() == 1
    assert len(calls) == 1
