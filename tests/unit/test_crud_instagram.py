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


# --- get_customer_for_account -------------------------------------------------


def test_get_customer_for_account_returns_matching_customer(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")

    found = crud_instagram.get_customer_for_account(db_session, account.id, customer.id)

    assert found.id == customer.id


def test_get_customer_for_account_raises_for_wrong_account(db_session, account, other_user):
    other_account = crud_instagram.upsert_account(
        db_session, other_user.id, "ig_other", "tok_other", "other_shop"
    )
    customer = crud_instagram.get_or_create_customer(db_session, other_account.id, "igsid_1")

    with pytest.raises(crud_instagram.CustomerNotFoundError):
        crud_instagram.get_customer_for_account(db_session, account.id, customer.id)


def test_get_customer_for_account_raises_for_missing_customer(db_session, account):
    with pytest.raises(crud_instagram.CustomerNotFoundError):
        crud_instagram.get_customer_for_account(db_session, account.id, 999999)


# --- get_conversations_for_account --------------------------------------------


def test_get_conversations_for_account_excludes_customers_with_no_messages(db_session, account):
    crud_instagram.get_or_create_customer(db_session, account.id, "igsid_silent")

    assert crud_instagram.get_conversations_for_account(db_session, account.id) == []


def test_get_conversations_for_account_returns_last_message_and_state(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1", "commenter1")
    crud_instagram.get_or_create_conversation(db_session, customer.id)
    crud_instagram.create_direct_message(db_session, account.id, customer.id, "inbound", "hi")
    crud_instagram.create_direct_message(db_session, account.id, customer.id, "outbound", "hello!")

    conversations = crud_instagram.get_conversations_for_account(db_session, account.id)

    assert len(conversations) == 1
    convo = conversations[0]
    assert convo["customer_id"] == customer.id
    assert convo["username"] == "commenter1"
    assert convo["last_message_body"] == "hello!"
    assert convo["last_message_direction"] == "outbound"
    assert convo["state"] == "NEGOTIATING"


def test_get_conversations_for_account_orders_most_recent_first(db_session, account):
    older = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_older")
    newer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_newer")
    crud_instagram.create_direct_message(db_session, account.id, older.id, "inbound", "first ever")
    crud_instagram.create_direct_message(db_session, account.id, newer.id, "inbound", "just now")

    conversations = crud_instagram.get_conversations_for_account(db_session, account.id)

    assert [c["customer_id"] for c in conversations] == [newer.id, older.id]


def test_get_conversations_for_account_scoped_to_account(db_session, account, other_user):
    other_account = crud_instagram.upsert_account(
        db_session, other_user.id, "ig_other2", "tok_other2", "other_shop2"
    )
    mine = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_mine")
    theirs = crud_instagram.get_or_create_customer(db_session, other_account.id, "igsid_theirs")
    crud_instagram.create_direct_message(db_session, account.id, mine.id, "inbound", "hi")
    crud_instagram.create_direct_message(db_session, other_account.id, theirs.id, "inbound", "hi")

    conversations = crud_instagram.get_conversations_for_account(db_session, account.id)

    assert [c["customer_id"] for c in conversations] == [mine.id]


# --- get_pending_handoffs_for_account / resume_conversation -------------------


def test_get_pending_handoffs_for_account_only_returns_handoff_state(db_session, account):
    negotiating = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_neg")
    crud_instagram.get_or_create_conversation(db_session, negotiating.id)

    handed_off = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_ho", "needs_help")
    context = crud_instagram.get_or_create_conversation(db_session, handed_off.id)
    context.state = ConversationState.HUMAN_HANDOFF
    db_session.commit()
    crud_instagram.create_direct_message(db_session, account.id, handed_off.id, "inbound", "I need a human")

    handoffs = crud_instagram.get_pending_handoffs_for_account(db_session, account.id)

    assert len(handoffs) == 1
    assert handoffs[0]["customer_id"] == handed_off.id
    assert handoffs[0]["username"] == "needs_help"
    assert handoffs[0]["last_message_body"] == "I need a human"


def test_resume_conversation_flips_state_back_to_negotiating(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")
    context = crud_instagram.get_or_create_conversation(db_session, customer.id)
    context.state = ConversationState.HUMAN_HANDOFF
    context.handoff_at = None
    db_session.commit()

    crud_instagram.resume_conversation(db_session, customer.id)

    db_session.refresh(context)
    assert context.state == ConversationState.NEGOTIATING
    assert context.handoff_at is None


def test_resume_conversation_raises_if_not_in_handoff(db_session, account):
    customer = crud_instagram.get_or_create_customer(db_session, account.id, "igsid_1")
    crud_instagram.get_or_create_conversation(db_session, customer.id)

    with pytest.raises(crud_instagram.ConversationNotInHandoffError):
        crud_instagram.resume_conversation(db_session, customer.id)
