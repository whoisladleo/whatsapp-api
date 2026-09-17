from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.models import InstagramAccount, InstagramComment


def get_account_by_ig_business_id(
    db: Session, ig_business_id: str
) -> Optional[InstagramAccount]:
    return (
        db.query(InstagramAccount)
        .filter(InstagramAccount.ig_business_id == ig_business_id)
        .first()
    )


def get_accounts_by_user(db: Session, user_id: int) -> List[InstagramAccount]:
    return db.query(InstagramAccount).filter(InstagramAccount.user_id == user_id).all()


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
