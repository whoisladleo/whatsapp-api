"""Operator allow-list unit tests (#75).

The critical invariant lives here: only VERIFIED identities grant
operator power — User.username (OTP path) or a Meta-OAuth-linked
InstagramAccount handle. User.phone_number is self-reported and must
never match, or any IG-login user could claim an operator's number.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.core.config import settings
from app.db.models import InstagramAccount, SupportAccessEvent, SupportAccessKind, User
from app.services import support_access
from app.services.support_access import IMPERSONATOR_CLAIM


@pytest.fixture(autouse=True)
def _operators(monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", "255700000001, 0711222333")
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", "@WhoIsLadLeo, other.handle")


def _user(db_session, **kwargs):
    user = User(**kwargs)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _link_instagram(db_session, user, username):
    db_session.add(
        InstagramAccount(
            ig_business_id=f"ig_{username}_{user.id.hex[:6]}",
            access_token="tok", username=username, user_id=user.id,
        )
    )
    db_session.commit()


def test_verified_phone_matches_allow_list(db_session):
    user = _user(db_session, username="255700000001")

    assert support_access.operator_match(db_session, user) == "255700000001"
    assert support_access.is_support_operator(db_session, user) is True


def test_phone_matching_normalizes_formats(db_session):
    # Allow-listed as 0711222333, stored normalized — both sides collapse.
    user = _user(db_session, username="255711222333")

    assert support_access.is_support_operator(db_session, user) is True


def test_self_reported_phone_grants_nothing(db_session):
    attacker = _user(db_session, username=None, phone_number="255700000001")

    assert support_access.operator_match(db_session, attacker) is None
    assert support_access.is_support_operator(db_session, attacker) is False


def test_ordinary_merchant_is_not_operator(db_session):
    user = _user(db_session, username="255799988877")

    assert support_access.is_support_operator(db_session, user) is False


def test_no_allow_list_means_nobody(db_session, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_PHONES", None)
    monkeypatch.setattr(settings, "SUPPORT_OPERATOR_INSTAGRAMS", None)
    user = _user(db_session, username="255700000001")

    assert support_access.is_support_operator(db_session, user) is False


def test_linked_instagram_handle_matches(db_session):
    user = _user(db_session, username=None)
    _link_instagram(db_session, user, "whoisladleo")

    assert support_access.operator_match(db_session, user) == "@whoisladleo"
    assert support_access.is_support_operator(db_session, user) is True


def test_instagram_match_ignores_case_and_at(db_session):
    user = _user(db_session, username=None)
    _link_instagram(db_session, user, "WhoIsLadLeo")

    assert support_access.is_support_operator(db_session, user) is True

    other = _user(db_session, username=None)
    _link_instagram(db_session, other, "Other.Handle")
    assert support_access.is_support_operator(db_session, other) is True


def test_unlinked_handle_grants_nothing(db_session):
    user = _user(db_session, username=None, name="whoisladleo")

    assert support_access.is_support_operator(db_session, user) is False


def test_target_label_prefers_verified_identity(db_session):
    by_phone = _user(db_session, username="255700000009", phone_number="255700000010")
    assert support_access.target_label(by_phone) == "255700000009"

    by_self_report = _user(db_session, username=None, phone_number="255700000011")
    assert support_access.target_label(by_self_report) == "255700000011"

    by_ig = _user(db_session, username=None)
    _link_instagram(db_session, by_ig, "shopowner")
    assert support_access.target_label(by_ig) == "@shopowner"


def test_impersonation_token_claims_and_expiry(db_session):
    operator = _user(db_session, username="255700000001")
    target = _user(db_session, username="255700000009")

    token = support_access.create_impersonation_token(target.id, operator.id)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    # sub is the TARGET: all existing tenant scoping just works.
    assert payload["sub"] == str(target.id)
    assert payload[IMPERSONATOR_CLAIM] == str(operator.id)
    exp = datetime.utcfromtimestamp(payload["exp"])
    assert timedelta(minutes=25) < exp - datetime.utcnow() < timedelta(minutes=35)


def test_record_support_event_never_raises(db_session):
    # operator_id None violates NOT NULL — must degrade to a log, not raise.
    support_access.record_support_event(
        db_session, None, uuid4(), SupportAccessKind.START
    )

    assert db_session.query(SupportAccessEvent).count() == 0
