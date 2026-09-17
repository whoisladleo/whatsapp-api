from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.models import (
    ConversationState,
    InstagramAccount,
    InstagramComment,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
)


class CustomerNotFoundError(Exception):
    """No InstagramCustomer with this id exists for this account."""


class ConversationNotInHandoffError(Exception):
    """The conversation exists but isn't currently in HUMAN_HANDOFF state."""


def get_account_by_ig_business_id(
    db: Session, ig_business_id: str
) -> Optional[InstagramAccount]:
    return (
        db.query(InstagramAccount)
        .filter(InstagramAccount.ig_business_id == ig_business_id)
        .first()
    )


def get_accounts_by_user(db: Session, user_id: int) -> List[InstagramAccount]:
    # access_token == "" marks a disconnected account (see disconnect_account)
    # — the row and its comment/DM/order history stick around, but it's no
    # longer "connected" from the merchant's point of view.
    return (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == user_id, InstagramAccount.access_token != "")
        .all()
    )


def disconnect_account(db: Session, account: InstagramAccount) -> None:
    """
    Clears local Meta access and pauses Amara without deleting the account
    row — InstagramCustomer/InstagramComment/InstagramDirectMessage/
    Order.instagram_account_id all reference it via required (non-nullable,
    no ON DELETE CASCADE) foreign keys, so a hard delete would fail for any
    account with real history. Reconnecting later (upsert_account) finds and
    revives this same row rather than creating a duplicate.
    """
    account.access_token = ""
    account.ai_enabled = False
    db.commit()


def comment_already_processed(db: Session, comment_id: str) -> bool:
    return (
        db.query(InstagramComment)
        .filter(InstagramComment.comment_id == comment_id)
        .first()
        is not None
    )


def upsert_account(
    db: Session, user_id: int, ig_business_id: str, access_token: str, username: Optional[str]
) -> InstagramAccount:
    """
    One Instagram connection per Taskflow user, enforced here rather than at
    the DB level — reconnecting always replaces whichever account this user
    had connected before, even if it's a *different* physical Instagram
    account than last time (Instagram's OAuth screen authorizes whatever
    account is currently active in the browser, which a user can easily
    switch between reconnects). Any other rows already on file for this
    user are dropped as leftover duplicates.
    """
    existing_for_user = (
        db.query(InstagramAccount).filter(InstagramAccount.user_id == user_id).all()
    )

    account = next(
        (a for a in existing_for_user if a.ig_business_id == ig_business_id), None
    )
    if not account and existing_for_user:
        account = existing_for_user[0]
    for other in existing_for_user:
        if account is None or other.id != account.id:
            db.delete(other)

    if not account:
        # This exact Instagram account may already be connected under a
        # different Taskflow user — the most recent OAuth consent is proof
        # of current control, so it takes over the row rather than erroring.
        account = get_account_by_ig_business_id(db, ig_business_id)

    if account:
        account.ig_business_id = ig_business_id
        account.access_token = access_token
        account.username = username
        account.user_id = user_id
    else:
        account = InstagramAccount(
            ig_business_id=ig_business_id,
            access_token=access_token,
            username=username,
            user_id=user_id,
        )
        db.add(account)

    db.commit()
    db.refresh(account)
    return account


def create_comment(
    db: Session, account_id: int, item: Dict[str, Any]
) -> InstagramComment:
    comment = InstagramComment(
        account_id=account_id,
        comment_id=item["comment_id"],
        media_id=item.get("media_id"),
        from_username=item.get("from_username"),
        from_ig_scoped_id=item.get("from_ig_scoped_id"),
        body=item.get("body"),
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def dm_already_processed(db: Session, ig_message_id: Optional[str]) -> bool:
    if not ig_message_id:
        return False
    return (
        db.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.ig_message_id == ig_message_id)
        .first()
        is not None
    )


def get_or_create_customer(
    db: Session, account_id: int, ig_scoped_id: str, username: Optional[str] = None
) -> InstagramCustomer:
    customer = (
        db.query(InstagramCustomer)
        .filter(InstagramCustomer.ig_scoped_id == ig_scoped_id)
        .first()
    )
    if customer:
        if username and customer.username != username:
            customer.username = username
            db.commit()
            db.refresh(customer)
        return customer

    customer = InstagramCustomer(
        account_id=account_id, ig_scoped_id=ig_scoped_id, username=username
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def get_or_create_conversation(
    db: Session, customer_id: int
) -> InstagramConversationContext:
    context = (
        db.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer_id)
        .first()
    )
    if context:
        return context

    context = InstagramConversationContext(customer_id=customer_id)
    db.add(context)
    db.commit()
    db.refresh(context)
    return context


def get_dm_history(
    db: Session, customer_id: int, limit: int = 20
) -> List[InstagramDirectMessage]:
    return (
        db.query(InstagramDirectMessage)
        .filter(InstagramDirectMessage.customer_id == customer_id)
        .order_by(InstagramDirectMessage.id.desc())
        .limit(limit)
        .all()
    )[::-1]


def create_direct_message(
    db: Session,
    account_id: int,
    customer_id: int,
    direction: str,
    body: Optional[str],
    ig_message_id: Optional[str] = None,
) -> InstagramDirectMessage:
    message = InstagramDirectMessage(
        account_id=account_id,
        customer_id=customer_id,
        direction=direction,
        body=body,
        ig_message_id=ig_message_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def get_customer_for_account(
    db: Session, account_id: int, customer_id: int
) -> InstagramCustomer:
    """Ownership-checked lookup used by every dashboard endpoint below —
    raises rather than returning None so callers can 404 without an extra
    `if not customer` branch at each call site."""
    customer = (
        db.query(InstagramCustomer)
        .filter(InstagramCustomer.id == customer_id, InstagramCustomer.account_id == account_id)
        .first()
    )
    if not customer:
        raise CustomerNotFoundError(f"No conversation found for customer {customer_id}.")
    return customer


def get_conversations_for_account(db: Session, account_id: int) -> List[Dict[str, Any]]:
    """
    Every Instagram customer this account has exchanged DMs with, most
    recent activity first. Unlike WhatsApp's equivalent (crud/message.py),
    InstagramCustomer has a direct account_id FK, so no inference from
    message history is needed to scope this to the right account.
    """
    customers = (
        db.query(InstagramCustomer).filter(InstagramCustomer.account_id == account_id).all()
    )

    results = []
    for customer in customers:
        last_message = (
            db.query(InstagramDirectMessage)
            .filter(InstagramDirectMessage.customer_id == customer.id)
            .order_by(InstagramDirectMessage.id.desc())
            .first()
        )
        if not last_message:
            continue

        context = (
            db.query(InstagramConversationContext)
            .filter(InstagramConversationContext.customer_id == customer.id)
            .first()
        )

        results.append(
            {
                "customer_id": customer.id,
                "username": customer.username,
                "last_activity_at": last_message.created_at,
                "last_message_body": last_message.body,
                "last_message_direction": last_message.direction,
                "state": context.state.value if context else None,
            }
        )

    results.sort(key=lambda r: r["last_activity_at"], reverse=True)
    return results


def get_pending_handoffs_for_account(db: Session, account_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.query(InstagramConversationContext, InstagramCustomer)
        .join(InstagramCustomer, InstagramConversationContext.customer_id == InstagramCustomer.id)
        .filter(
            InstagramConversationContext.state == ConversationState.HUMAN_HANDOFF,
            InstagramCustomer.account_id == account_id,
        )
        .all()
    )

    results = []
    for context, customer in rows:
        last_message = (
            db.query(InstagramDirectMessage)
            .filter(InstagramDirectMessage.customer_id == customer.id)
            .order_by(InstagramDirectMessage.id.desc())
            .first()
        )
        results.append(
            {
                "customer_id": customer.id,
                "username": customer.username,
                "handoff_at": context.handoff_at,
                "last_message_body": last_message.body if last_message else None,
            }
        )
    return results


def resume_conversation(db: Session, customer_id: int) -> None:
    """Flips a HUMAN_HANDOFF conversation back to NEGOTIATING — WhatsApp
    counterpart is crud/message.py's resume_conversation_for_account. No
    account-ownership check here since the caller (POST
    /instagram/handoffs/{customer_id}/resume) already resolved customer_id
    via get_customer_for_account first."""
    context = (
        db.query(InstagramConversationContext)
        .filter(InstagramConversationContext.customer_id == customer_id)
        .first()
    )
    if not context or context.state != ConversationState.HUMAN_HANDOFF:
        raise ConversationNotInHandoffError(
            f"Customer {customer_id} is not currently in a human handoff state."
        )

    context.state = ConversationState.NEGOTIATING
    context.handoff_at = None
    db.commit()
