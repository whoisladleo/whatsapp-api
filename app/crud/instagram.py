from typing import Any, Dict, Optional

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


def comment_already_processed(db: Session, comment_id: str) -> bool:
    return (
        db.query(InstagramComment)
        .filter(InstagramComment.comment_id == comment_id)
        .first()
        is not None
    )


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
