import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

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


async def _no_display_name(account, igsid):
    """Stub for app.api.v1.instagram._fetch_instagram_display_name — used by
    tests that aren't exercising the display-name lookup itself, so they
    don't make a real outbound HTTP call."""
    return None


@pytest.fixture
def merchant_owner(db_session):
    owner = User(username="255700000099")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture(autouse=True)
def _no_eager_token_refresh(monkeypatch):
    """
    Fixture accounts have no token_refreshed_at (stale by definition), so
    without this every webhook post would run the refresh task inline in
    eager mode and attempt real HTTP. Neutralize it file-wide; the two
    trigger tests below override with their own recorder.
    """
    import app.tasks.instagram_token_refresh as refresh_module

    monkeypatch.setattr(
        refresh_module.refresh_account_token, "apply_async",
        lambda args=None, **kw: None,
    )


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


def test_comment_dispatch_failure_rolls_back_and_retry_replays(
    client, db_session, merchant_owner
):
    account = InstagramAccount(
        ig_business_id="ig_business_rb",
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

    def flaky(args=None, **kw):
        if not calls:
            calls.append("boom")
            raise RuntimeError("broker down")
        calls.append(args)

    task_module.process_inbound_comment.apply_async = flaky
    try:
        payload = _comment_payload("ig_business_rb", "comment_rb")
        with pytest.raises(RuntimeError, match="broker down"):
            client.post(WEBHOOK_URL, json=payload)
        assert (
            db_session.query(InstagramComment)
            .filter(InstagramComment.comment_id == "comment_rb")
            .count()
            == 0
        )
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_comment.apply_async = original

    assert resp.status_code == 200
    comment = (
        db_session.query(InstagramComment)
        .filter(InstagramComment.comment_id == "comment_rb")
        .first()
    )
    assert comment is not None
    assert calls[1] == [str(account.id), str(comment.id)]


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
    client, db_session, merchant_owner, monkeypatch
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

    # Not under test here — see test_new_dm_customer_gets_display_name_backfilled
    # and test_display_name_lookup_failure_does_not_block_dm_processing below.
    import app.api.v1.instagram as instagram_router
    monkeypatch.setattr(instagram_router, "_fetch_instagram_display_name", _no_display_name)

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


def test_dm_dispatch_failure_rolls_back_and_retry_replays(
    client, db_session, merchant_owner, monkeypatch
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_rb",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    import app.api.v1.instagram as instagram_router
    monkeypatch.setattr(instagram_router, "_fetch_instagram_display_name", _no_display_name)

    import app.tasks.instagram_dm_processing as task_module

    calls = []
    original = task_module.process_inbound_dm.apply_async

    def flaky(args=None, **kw):
        if not calls:
            calls.append("boom")
            raise RuntimeError("broker down")
        calls.append(args)

    task_module.process_inbound_dm.apply_async = flaky
    try:
        payload = _dm_payload("ig_business_dm_rb", "mid_rb")
        with pytest.raises(RuntimeError, match="broker down"):
            client.post(WEBHOOK_URL, json=payload)
        assert (
            db_session.query(InstagramDirectMessage)
            .filter(InstagramDirectMessage.ig_message_id == "mid_rb")
            .count()
            == 0
        )
        resp = client.post(WEBHOOK_URL, json=payload)
    finally:
        task_module.process_inbound_dm.apply_async = original

    assert resp.status_code == 200
    dm = (
        db_session.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.ig_message_id == "mid_rb")
        .first()
    )
    assert dm is not None
    assert calls[1][0] == str(account.id)
    assert calls[1][2] == str(dm.id)


def test_known_account_ai_disabled_stores_dm_without_dispatch(
    client, db_session, merchant_owner, monkeypatch
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_2",
        access_token="ig-tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.api.v1.instagram as instagram_router
    monkeypatch.setattr(instagram_router, "_fetch_instagram_display_name", _no_display_name)

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
    client, db_session, merchant_owner, monkeypatch
):
    account = InstagramAccount(
        ig_business_id="ig_business_dm_3",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    import app.api.v1.instagram as instagram_router
    monkeypatch.setattr(instagram_router, "_fetch_instagram_display_name", _no_display_name)

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


# --- display-name backfill for new DM customers -----------------------------


@respx.mock
def test_new_dm_customer_gets_display_name_backfilled(client, db_session, merchant_owner, monkeypatch):
    """
    The messaging webhook only ever gives us the sender's IGSID (see
    _dm_payload) — unlike the comments webhook, which includes from.username
    directly — so a brand-new InstagramCustomer is looked up via the Graph
    API and given a real display name instead of staying "Instagram user"
    in the dashboard forever.
    """
    account = InstagramAccount(
        ig_business_id="ig_business_dm_4",
        access_token="ig-tok",
        ai_enabled=False,  # keep this test focused; dispatch is covered elsewhere
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    respx.get(url__regex=r"https://graph\.instagram\.com/.+/igsid_dm_1").mock(
        return_value=httpx.Response(200, json={"id": "igsid_dm_1", "name": "Jane Doe", "username": "janedoe"})
    )

    payload = _dm_payload("ig_business_dm_4", "mid_name_1")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    customer = db_session.query(InstagramCustomer).filter(
        InstagramCustomer.ig_scoped_id == "igsid_dm_1"
    ).first()
    assert customer is not None
    assert customer.username == "janedoe"


@respx.mock
def test_display_name_lookup_failure_does_not_block_dm_processing(client, db_session, merchant_owner):
    """A failed/errored profile lookup must not stop the DM itself from
    being stored — worst case the customer just keeps showing as
    "Instagram user" in the dashboard."""
    account = InstagramAccount(
        ig_business_id="ig_business_dm_5",
        access_token="ig-tok",
        ai_enabled=False,
        user_id=merchant_owner.id,
    )
    db_session.add(account)
    db_session.commit()

    respx.get(url__regex=r"https://graph\.instagram\.com/.+/igsid_dm_1").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Unsupported request"}})
    )

    payload = _dm_payload("ig_business_dm_5", "mid_name_2")
    resp = client.post(WEBHOOK_URL, json=payload)

    assert resp.status_code == 200
    dm = db_session.query(InstagramDirectMessage).filter(
        InstagramDirectMessage.ig_message_id == "mid_name_2"
    ).first()
    assert dm is not None
    customer = db_session.query(InstagramCustomer).filter(
        InstagramCustomer.ig_scoped_id == "igsid_dm_1"
    ).first()
    assert customer is not None
    assert customer.username is None


# --- lazy token refresh dispatch (#80) ----------------------------------------


def _stale_and_fresh_accounts(db_session, merchant_owner, prefix):
    stale = InstagramAccount(
        ig_business_id=f"{prefix}_stale",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
        token_refreshed_at=datetime.utcnow() - timedelta(days=51),
    )
    fresh = InstagramAccount(
        ig_business_id=f"{prefix}_fresh",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
        token_refreshed_at=datetime.utcnow(),
    )
    db_session.add(stale)
    db_session.add(fresh)
    db_session.commit()
    db_session.refresh(stale)
    db_session.refresh(fresh)
    return stale, fresh


def test_comment_inbound_dispatches_refresh_only_when_stale(
    client, db_session, merchant_owner
):
    stale, fresh = _stale_and_fresh_accounts(db_session, merchant_owner, "ig_trig_c")
    null_stamp = InstagramAccount(
        ig_business_id="ig_trig_c_null",
        access_token="ig-tok",
        ai_enabled=True,
        user_id=merchant_owner.id,
        token_refreshed_at=None,
    )
    db_session.add(null_stamp)
    db_session.commit()
    db_session.refresh(null_stamp)

    import app.tasks.instagram_comment_processing as task_module
    import app.tasks.instagram_token_refresh as refresh_module

    refresh_calls = []
    orig_refresh = refresh_module.refresh_account_token.apply_async
    refresh_module.refresh_account_token.apply_async = (
        lambda args=None, **kw: refresh_calls.append(args)
    )
    orig_task = task_module.process_inbound_comment.apply_async
    task_module.process_inbound_comment.apply_async = lambda args=None, **kw: None
    try:
        assert client.post(
            WEBHOOK_URL, json=_comment_payload("ig_trig_c_stale", "c_trig_1")
        ).status_code == 200
        assert client.post(
            WEBHOOK_URL, json=_comment_payload("ig_trig_c_fresh", "c_trig_2")
        ).status_code == 200
        assert client.post(
            WEBHOOK_URL, json=_comment_payload("ig_trig_c_null", "c_trig_3")
        ).status_code == 200
    finally:
        refresh_module.refresh_account_token.apply_async = orig_refresh
        task_module.process_inbound_comment.apply_async = orig_task

    assert refresh_calls == [[str(stale.id)], [str(null_stamp.id)]]


def test_dm_inbound_dispatches_refresh_only_when_stale(
    client, db_session, merchant_owner, monkeypatch
):
    stale, fresh = _stale_and_fresh_accounts(db_session, merchant_owner, "ig_trig_d")

    import app.api.v1.instagram as instagram_router
    monkeypatch.setattr(instagram_router, "_fetch_instagram_display_name", _no_display_name)

    import app.tasks.instagram_dm_processing as task_module
    import app.tasks.instagram_token_refresh as refresh_module

    refresh_calls = []
    orig_refresh = refresh_module.refresh_account_token.apply_async
    refresh_module.refresh_account_token.apply_async = (
        lambda args=None, **kw: refresh_calls.append(args)
    )
    orig_task = task_module.process_inbound_dm.apply_async
    task_module.process_inbound_dm.apply_async = lambda args=None, **kw: None
    try:
        assert client.post(
            WEBHOOK_URL, json=_dm_payload("ig_trig_d_stale", "mid_trig_1")
        ).status_code == 200
        assert client.post(
            WEBHOOK_URL, json=_dm_payload("ig_trig_d_fresh", "mid_trig_2")
        ).status_code == 200
    finally:
        refresh_module.refresh_account_token.apply_async = orig_refresh
        task_module.process_inbound_dm.apply_async = orig_task

    assert refresh_calls == [[str(stale.id)]]


def test_unsigned_webhook_rejected_when_secret_unset_and_flag_off(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", None)
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", False)

    resp = client.post(WEBHOOK_URL, json=_comment_payload("ig_x", "c_x"))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Webhook signature verification is not configured"
