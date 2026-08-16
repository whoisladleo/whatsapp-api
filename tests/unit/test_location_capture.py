import pytest

import app.tasks.location_capture as location_capture
from app.db.models import Customer, Message, User, WhatsAppAccount


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    monkeypatch.setattr(location_capture, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000850")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = WhatsAppAccount(
        name="Loc Shop", phone_number_id="LOC_PHONE", access_token="tok", user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def customer(db_session):
    c = Customer(phone_number="255722223333")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _mock_send_text(monkeypatch, ok=True):
    async def fake(self, to, body, preview_url=False):
        if not ok:
            raise RuntimeError("network down")
        return {"messages": [{"id": "wamid.loc"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake)


def test_capture_location_no_matching_customer(account):
    result = location_capture.capture_customer_location(
        account.id, "255799999999", -6.7924, 39.2083
    )
    assert result == "No matching customer."


def test_capture_location_updates_named_place_and_notifies(db_session, account, customer, monkeypatch):
    _mock_send_text(monkeypatch)

    result = location_capture.capture_customer_location(
        account.id, customer.phone_number, -6.7924, 39.2083, "My Shop", "123 Main St"
    )

    assert result == "Success"
    customer = db_session.query(Customer).filter(Customer.phone_number == "255722223333").first()
    assert customer.delivery_latitude == -6.7924
    assert customer.delivery_longitude == 39.2083
    assert customer.delivery_address == "My Shop"

    logged = (
        db_session.query(Message)
        .filter(Message.to_phone == customer.phone_number, Message.direction == "outbound")
        .first()
    )
    assert logged is not None
    assert logged.status == "sent"


def test_capture_raw_pin_does_not_overwrite_address(db_session, account, customer, monkeypatch):
    customer.delivery_address = "Existing address on file"
    db_session.commit()
    _mock_send_text(monkeypatch)

    location_capture.capture_customer_location(
        account.id, customer.phone_number, -6.7924, 39.2083
    )

    customer = db_session.query(Customer).filter(Customer.phone_number == "255722223333").first()
    assert customer.delivery_address == "Existing address on file"
    assert customer.delivery_latitude == -6.7924
    assert customer.delivery_longitude == 39.2083


def test_capture_location_accepts_unnormalized_phone_format(db_session, account, customer, monkeypatch):
    _mock_send_text(monkeypatch)
    local_format = "0" + customer.phone_number[3:]  # 255722223333 -> 0722223333

    result = location_capture.capture_customer_location(
        account.id, local_format, -6.7924, 39.2083
    )

    assert result == "Success"
    customer = db_session.query(Customer).filter(Customer.phone_number == "255722223333").first()
    assert customer.delivery_latitude == -6.7924


def test_capture_location_no_account_still_saves(db_session, customer):
    result = location_capture.capture_customer_location(
        999999, customer.phone_number, -6.7924, 39.2083
    )

    assert result == "Location saved; no account to send a confirmation from."
    customer = db_session.query(Customer).filter(Customer.phone_number == "255722223333").first()
    assert customer.delivery_latitude == -6.7924


def test_capture_location_send_failure_still_logs(db_session, account, customer, monkeypatch):
    phone = customer.phone_number
    _mock_send_text(monkeypatch, ok=False)

    result = location_capture.capture_customer_location(
        account.id, phone, -6.7924, 39.2083
    )

    assert result == "Success"
    logged = (
        db_session.query(Message)
        .filter(Message.to_phone == phone, Message.direction == "outbound")
        .first()
    )
    assert logged.status == "failed"
