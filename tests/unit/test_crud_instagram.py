import pytest

from app.crud import instagram as crud_instagram
from app.db.models import (
    ConversationState,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    User,
)


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


@pytest.fixture
def account(db_session, user):
    return crud_instagram.upsert_account(db_session, user.id, "ig_1", "tok_1", "shop_one")


def test_get_or_create_customer_creates_new(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1", "commenter1")

    assert customer.ig_scoped_id == "igsid_1"
    assert customer.username == "commenter1"
    assert customer.account_id == account.id
    assert db_session.query(InstagramCustomer).count() == 1


def test_get_or_create_customer_returns_existing_and_updates_username(db_session, account):
    first = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1", "old_name")
    second = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1", "new_name")

    assert second.id == first.id
    assert second.username == "new_name"
    assert db_session.query(InstagramCustomer).count() == 1


def test_get_or_create_conversation_creates_with_default_state(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")

    context = crud_instagram.get_or_create_conversation(db_session, customer.id)

    assert context.customer_id == customer.id
    assert context.state == ConversationState.NEGOTIATING
    assert db_session.query(InstagramConversationContext).count() == 1


def test_get_or_create_conversation_returns_existing(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")
    first = crud_instagram.get_or_create_conversation(db_session, customer.id)
    first.state = ConversationState.HUMAN_HANDOFF
    db_session.commit()

    second = crud_instagram.get_or_create_conversation(db_session, customer.id)

    assert second.id == first.id
    assert second.state == ConversationState.HUMAN_HANDOFF
    assert db_session.query(InstagramConversationContext).count() == 1


def test_dm_already_processed(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")
    assert crud_instagram.dm_already_processed(db_session, "mid_1") is False

    crud_instagram.create_direct_message(
        db_session, account.id, customer.id, "inbound", "hi", ig_message_id="mid_1"
    )

    assert crud_instagram.dm_already_processed(db_session, "mid_1") is True
    assert crud_instagram.dm_already_processed(db_session, None) is False


def test_create_direct_message_and_get_dm_history_orders_oldest_first(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")

    crud_instagram.create_direct_message(
        db_session, account.id, customer.id, "inbound", "first", ig_message_id="mid_1"
    )
    crud_instagram.create_direct_message(
        db_session, account.id, customer.id, "outbound", "second", ig_message_id=None
    )
    crud_instagram.create_direct_message(
        db_session, account.id, customer.id, "inbound", "third", ig_message_id="mid_3"
    )

    history = crud_instagram.get_dm_history(db_session, customer.id)

    assert [m.body for m in history] == ["first", "second", "third"]
    assert db_session.query(InstagramDirectMessage).count() == 3
