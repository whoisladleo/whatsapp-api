"""Lazy IG token refresh (#80): staleness rule, refresh task, dispatch claim."""

import uuid
from datetime import datetime, timedelta

import httpx
import pytest
import respx

import app.tasks.instagram_token_refresh as task_module
from app.crud import instagram as crud_instagram
from app.db.models import InstagramAccount, User

REFRESH_URL_RE = r"https://graph\.instagram\.com/refresh_access_token"


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


def _account(db_session, owner, stamp):
    acct = InstagramAccount(
        ig_business_id=f"ig_biz_{uuid.uuid4().hex[:8]}",
        access_token="old-tok",
        username="shopowner",
        ai_enabled=True,
        user_id=owner.id,
        token_refreshed_at=stamp,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


def _reload(db_session, account_id):
    return (
        db_session.query(InstagramAccount)
        .filter(InstagramAccount.id == uuid.UUID(str(account_id)))
        .first()
    )


def test_needs_refresh_matrix():
    assert crud_instagram.token_needs_refresh(
        InstagramAccount(access_token="", token_refreshed_at=None)
    ) is False
    assert crud_instagram.token_needs_refresh(
        InstagramAccount(access_token="t", token_refreshed_at=None)
    ) is True
    assert crud_instagram.token_needs_refresh(
        InstagramAccount(
            access_token="t",
            token_refreshed_at=datetime.utcnow() - timedelta(days=51),
        )
    ) is True
    assert crud_instagram.token_needs_refresh(
        InstagramAccount(access_token="t", token_refreshed_at=datetime.utcnow())
    ) is False


@respx.mock
def test_refresh_task_updates_token_and_stamp(db_session, owner):
    account = _account(db_session, owner, datetime.utcnow() - timedelta(days=51))
    # The task's `finally: db.close()` detaches `account` (it shares this
    # session) — stash the id first, then re-query rather than refresh(),
    # same as the DM task tests do for the same reason.
    account_id = str(account.id)
    route = respx.get(url__regex=REFRESH_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "new-tok"})
    )

    result = task_module.refresh_account_token(account_id)

    assert result == "Refreshed."
    assert route.calls.last.request.url.params["grant_type"] == "ig_refresh_token"
    account = _reload(db_session, account_id)
    assert account.access_token == "new-tok"
    assert datetime.utcnow() - account.token_refreshed_at < timedelta(minutes=5)


@respx.mock
def test_refresh_task_skips_fresh_account(db_session, owner):
    account = _account(db_session, owner, datetime.utcnow())
    route = respx.get(url__regex=REFRESH_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "new-tok"})
    )

    assert task_module.refresh_account_token(str(account.id)) == "Token fresh; skipping."
    assert not route.called


@respx.mock
def test_refresh_task_second_run_skips_on_claim(db_session, owner):
    account = _account(db_session, owner, datetime.utcnow() - timedelta(days=51))
    account_id = str(account.id)
    route = respx.get(url__regex=REFRESH_URL_RE).mock(
        return_value=httpx.Response(200, json={"access_token": "new-tok"})
    )

    assert task_module.refresh_account_token(account_id) == "Refreshed."
    assert task_module.refresh_account_token(account_id) in (
        "Token fresh; skipping.",
        "Already refreshed; skipping duplicate dispatch.",
    )
    assert route.call_count == 1


@respx.mock
def test_refresh_task_failure_restores_stamp(db_session, owner):
    old_stamp = datetime.utcnow() - timedelta(days=51)
    account = _account(db_session, owner, old_stamp)
    account_id = str(account.id)
    respx.get(url__regex=REFRESH_URL_RE).mock(
        return_value=httpx.Response(400, json={"error": {"message": "expired"}})
    )

    result = task_module.refresh_account_token(account_id)

    assert result == "Refresh failed; stamp restored."
    account = _reload(db_session, account_id)
    assert account.access_token == "old-tok"
    assert account.token_refreshed_at == old_stamp


def test_refresh_task_unknown_and_disconnected(db_session, owner):
    assert task_module.refresh_account_token(str(uuid.uuid4())) == (
        "Account gone or disconnected."
    )
    assert task_module.refresh_account_token("not-a-uuid") == "Account not found."

    account = _account(db_session, owner, None)
    account.access_token = ""
    db_session.commit()
    assert task_module.refresh_account_token(str(account.id)) == "Account gone or disconnected."
