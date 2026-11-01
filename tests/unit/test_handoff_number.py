"""Handoff-number resolution (#105): explicit override wins, else OTP
username, else profile phone; non-dialable values are skipped."""

from app.crud.user import resolve_handoff_number
from app.db.models import User


def _user(**fields):
    return User(**fields)


def test_explicit_handoff_number_wins_over_username():
    user = _user(username="255700000001", phone_number="255700000002",
                 handoff_phone_number="255700000003")

    assert resolve_handoff_number(user) == "255700000003"


def test_username_used_when_no_override():
    user = _user(username="255700000001", phone_number="255700000002",
                 handoff_phone_number=None)

    assert resolve_handoff_number(user) == "255700000001"


def test_profile_phone_used_when_no_username_or_override():
    user = _user(username=None, phone_number="255700000002", handoff_phone_number=None)

    assert resolve_handoff_number(user) == "255700000002"


def test_non_dialable_values_skipped():
    user = _user(username="whoisladleo", phone_number="", handoff_phone_number="  ")

    assert resolve_handoff_number(user) is None


def test_local_format_normalized():
    user = _user(username=None, phone_number=None, handoff_phone_number="0712345678")

    assert resolve_handoff_number(user) == "255712345678"
