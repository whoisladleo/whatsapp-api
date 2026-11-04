"""Verdict + dispatch + merchant lines for take-over replies (#149)."""

from datetime import datetime, timedelta

import pytest

from app.db.models import (
    Customer,
    InstagramAccount,
    InstagramCustomer,
    InstagramDirectMessage,
    Message,
    User,
    WhatsAppAccount,
)
from app.services import resume as resume_service


@pytest.fixture
def wa_setup(db_session):
    owner = User(username="255700000101")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    account = WhatsAppAccount(
        name="Shop", phone_number_id="PHONE_R", access_token="tok", user_id=owner.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    customer = Customer(phone_number="255722220001")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return account, customer


@pytest.fixture
def ig_setup(db_session):
    owner = User(username="255700000102")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    account = InstagramAccount(
        ig_business_id="ig_biz_r", access_token="ig-tok", username="shop", user_id=owner.id
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    customer = InstagramCustomer(
        ig_scoped_id="igsid_r", username="rider", account_id=account.id
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)
    return account, customer


def _wa_inbound(db_session, account, phone, body="hello?", age_hours=0):
    row = Message(
        from_phone=phone, type="text", body=body, direction="inbound",
        status="sent", account_id=account.id,
        timestamp=datetime.utcnow() - timedelta(hours=age_hours),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _ig_inbound(db_session, account, customer, body="hello?", age_hours=0):
    row = InstagramDirectMessage(
        account_id=account.id, customer_id=customer.id, direction="inbound", body=body,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_wa_verdict_answerable_carries_body(db_session, wa_setup):
    account, customer = wa_setup
    _wa_inbound(db_session, account, customer.phone_number, body="still there?")

    verdict = resume_service.verdict_for_whatsapp(db_session, account.id, customer.phone_number)

    assert verdict.answerable is True
    assert verdict.reason == "answerable"
    assert verdict.body == "still there?"


def test_wa_verdict_stale(db_session, wa_setup):
    account, customer = wa_setup
    _wa_inbound(db_session, account, customer.phone_number, age_hours=25)

    verdict = resume_service.verdict_for_whatsapp(db_session, account.id, customer.phone_number)

    assert verdict.answerable is False
    assert verdict.reason == "stale"


def test_wa_verdict_no_inbound_ignores_outbound(db_session, wa_setup):
    account, customer = wa_setup
    db_session.add(
        Message(
            to_phone=customer.phone_number, type="text", body="hi", direction="outbound",
            status="sent", account_id=account.id,
        )
    )
    db_session.commit()

    verdict = resume_service.verdict_for_whatsapp(db_session, account.id, customer.phone_number)

    assert verdict.answerable is False
    assert verdict.reason == "no_inbound"


def test_ig_verdict_answerable_carries_row(db_session, ig_setup):
    account, customer = ig_setup
    row = _ig_inbound(db_session, account, customer)

    verdict = resume_service.verdict_for_instagram(db_session, customer.id)

    assert verdict.answerable is True
    assert verdict.reason == "answerable"
    assert verdict.row_id == row.id
    assert verdict.account_id == account.id


def test_ig_verdict_stale(db_session, ig_setup):
    account, customer = ig_setup
    _ig_inbound(db_session, account, customer, age_hours=25)

    verdict = resume_service.verdict_for_instagram(db_session, customer.id)

    assert verdict.answerable is False
    assert verdict.reason == "stale"


def test_ig_verdict_no_inbound(db_session, ig_setup):
    _, customer = ig_setup

    verdict = resume_service.verdict_for_instagram(db_session, customer.id)

    assert verdict.answerable is False
    assert verdict.reason == "no_inbound"


def test_dispatch_queues_wa_turn(db_session, wa_setup, monkeypatch):
    account, customer = wa_setup
    _wa_inbound(db_session, account, customer.phone_number, body="ping")
    calls = []
    monkeypatch.setattr(
        "app.tasks.webhook_processing.process_inbound_message.apply_async",
        lambda *a, **kw: calls.append(kw.get("args")),
    )

    verdict = resume_service.verdict_for_whatsapp(db_session, account.id, customer.phone_number)
    assert resume_service.dispatch_resume_reply(verdict) is True

    assert calls == [[str(account.id), customer.phone_number, "ping"]]


def test_dispatch_queues_ig_turn_with_resume_flag(db_session, ig_setup, monkeypatch):
    account, customer = ig_setup
    row = _ig_inbound(db_session, account, customer)
    calls = []
    monkeypatch.setattr(
        "app.tasks.instagram_dm_processing.process_inbound_dm.apply_async",
        lambda *a, **kw: calls.append(kw.get("args")),
    )

    verdict = resume_service.verdict_for_instagram(db_session, customer.id)
    assert resume_service.dispatch_resume_reply(verdict) is True

    assert calls == [[str(account.id), str(customer.id), str(row.id), 0, None, 0, True]]


def test_dispatch_noop_unless_answerable(db_session, wa_setup, monkeypatch):
    account, customer = wa_setup
    calls = []
    monkeypatch.setattr(
        "app.tasks.webhook_processing.process_inbound_message.apply_async",
        lambda *a, **kw: calls.append(kw.get("args")),
    )

    verdict = resume_service.verdict_for_whatsapp(db_session, account.id, customer.phone_number)
    assert resume_service.dispatch_resume_reply(verdict) is False
    assert calls == []


def test_merchant_resume_lines():
    answerable = resume_service.ResumeVerdict("whatsapp", True, "answerable")
    stale = resume_service.ResumeVerdict("whatsapp", False, "stale")
    empty = resume_service.ResumeVerdict("whatsapp", False, "no_inbound")

    assert resume_service.merchant_resume_line("2557", "/reply 2557 <message>", answerable) == (
        "Conversation with 2557 resumed. Amara is replying to their last message now."
    )
    stale_line = resume_service.merchant_resume_line("2557", "/reply 2557 <message>", stale)
    assert "over 24 hours old" in stale_line and "/reply 2557 <message>" in stale_line
    assert "next message" in resume_service.merchant_resume_line("2557", "/reply 2557 <message>", empty)


def test_takeover_resume_lines():
    answerable = resume_service.ResumeVerdict("instagram", True, "answerable")
    stale = resume_service.ResumeVerdict("instagram", False, "stale")
    empty = resume_service.ResumeVerdict("instagram", False, "no_inbound")

    assert "replying to their last message now" in resume_service.takeover_resume_line(
        "@rider", "", "/reply @rider <message>", answerable
    )
    assert "over 24 hours old" in resume_service.takeover_resume_line(
        "@rider", "", "/reply @rider <message>", stale
    )
    assert "next message" in resume_service.takeover_resume_line(
        "@rider", "", "/reply @rider <message>", empty
    )
