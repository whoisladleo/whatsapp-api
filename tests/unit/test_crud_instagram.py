import pytest

from app.crud import instagram as crud_instagram
from app.db.models import InstagramAccount, User


@pytest.fixture
def user(db_session):
    u = User(username="255700000700")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def other_user(db_session):
    u = User(username="255700000701")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def test_upsert_creates_new_account(db_session, user):
    account = crud_instagram.upsert_account(
        db_session, user.id, "ig_1", "tok_1", "shop_one"
    )

    assert account.ig_business_id == "ig_1"
    assert account.access_token == "tok_1"
    assert account.username == "shop_one"
    assert account.user_id == user.id
    assert db_session.query(InstagramAccount).count() == 1


def test_upsert_reconnecting_same_account_updates_in_place(db_session, user):
    first = crud_instagram.upsert_account(db_session, user.id, "ig_1", "tok_old", "shop_one")
    second = crud_instagram.upsert_account(db_session, user.id, "ig_1", "tok_new", "shop_one_renamed")

    assert second.id == first.id
    assert second.access_token == "tok_new"
    assert second.username == "shop_one_renamed"
    assert db_session.query(InstagramAccount).count() == 1


def test_upsert_reconnecting_with_different_ig_account_replaces_not_duplicates(
    db_session, user
):
    """
    The exact bug this fix addresses: a user's OAuth screen can authorize a
    different physical Instagram account on a later reconnect (whichever
    one is active in their browser), which must replace — not add to —
    their one Taskflow-side connection.
    """
    first = crud_instagram.upsert_account(db_session, user.id, "ig_business", "tok_biz", "taskflow_os")
    second = crud_instagram.upsert_account(db_session, user.id, "ig_personal", "tok_personal", "whoisladleo")

    assert second.id == first.id
    assert second.ig_business_id == "ig_personal"
    assert second.username == "whoisladleo"
    assert db_session.query(InstagramAccount).count() == 1
    assert db_session.query(InstagramAccount).filter(
        InstagramAccount.ig_business_id == "ig_business"
    ).count() == 0


def test_upsert_cleans_up_preexisting_duplicate_rows_for_same_user(db_session, user):
    """Simulates the pre-fix broken state (two rows for one user) and
    confirms it self-heals on the next reconnect, regardless of which of
    the two accounts gets re-authorized."""
    db_session.add_all([
        InstagramAccount(ig_business_id="ig_a", access_token="tok_a", username="a", user_id=user.id),
        InstagramAccount(ig_business_id="ig_b", access_token="tok_b", username="b", user_id=user.id),
    ])
    db_session.commit()
    assert db_session.query(InstagramAccount).filter(InstagramAccount.user_id == user.id).count() == 2

    result = crud_instagram.upsert_account(db_session, user.id, "ig_a", "tok_a_new", "a")

    remaining = db_session.query(InstagramAccount).filter(InstagramAccount.user_id == user.id).all()
    assert len(remaining) == 1
    assert remaining[0].id == result.id
    assert remaining[0].ig_business_id == "ig_a"
    assert remaining[0].access_token == "tok_a_new"


def test_upsert_reassigns_account_already_connected_by_a_different_user(
    db_session, user, other_user
):
    """Most recent OAuth consent is treated as proof of current control."""
    crud_instagram.upsert_account(db_session, other_user.id, "ig_shared", "tok_1", "shared_shop")

    result = crud_instagram.upsert_account(db_session, user.id, "ig_shared", "tok_2", "shared_shop")

    assert result.user_id == user.id
    assert db_session.query(InstagramAccount).count() == 1
    assert db_session.query(InstagramAccount).filter(
        InstagramAccount.user_id == other_user.id
    ).count() == 0
