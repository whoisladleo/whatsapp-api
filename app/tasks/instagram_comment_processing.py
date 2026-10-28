import logging

from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.core.instagram_provider import InstagramProvider
from app.db.models import InstagramAccount, InstagramComment
from app.db.session import SessionLocal
from app.services import usage
from app.services.llm import llm_service
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


@celery_app.task
def process_inbound_comment(account_id: str, comment_row_id: str):
    """
    Generates and sends Amara's reply to an inbound Instagram comment: a
    public reply visible on the post, and a one-shot private-reply DM with
    the actual details. comment_row_id is InstagramComment.id, not Meta's
    own comment_id string.
    """
    db = SessionLocal()
    try:
        return _run_async(_process(db, account_id, comment_row_id))
    finally:
        db.close()


async def _process(db, account_id: str, comment_row_id: str) -> str:
    account_uuid = coerce_uuid(account_id)
    comment_uuid = coerce_uuid(comment_row_id)
    if account_uuid is None or comment_uuid is None:
        return "Account or comment not found."
    account = db.query(InstagramAccount).filter(InstagramAccount.id == account_uuid).first()
    comment = db.query(InstagramComment).filter(InstagramComment.id == comment_uuid).first()
    if not account or not comment:
        return "Account or comment not found."

    if llm_service is None:
        logger.warning("llm_service unavailable; skipping comment %s.", comment.comment_id)
        return "LLM unavailable."

    provider = InstagramProvider(account.access_token)

    # Best-effort parent-post fetch (B1): a failed lookup (deleted media,
    # revoked token, transport error) falls back to a media-unaware reply
    # rather than dropping the comment.
    post_media = None
    if comment.media_id:
        try:
            post_media = await provider.get_media(comment.media_id)
        except Exception as e:
            logger.warning(
                "Parent-media fetch for comment %s failed: %s", comment.comment_id, e
            )

    reply = await llm_service.generate_comment_reply(
        comment_body=comment.body or "",
        merchant_id=account.user_id,
        db=db,
        account_name=account.username or "the shop",
        post_media=post_media,
    )
    if not reply:
        logger.warning("No usable reply generated for comment %s.", comment.comment_id)
        return "No reply generated."

    public_response = await provider.reply_to_comment(comment.comment_id, reply["public_reply"])
    if "error" not in public_response:
        comment.public_reply_sent = True
        usage.record_reply(db, account.user_id, "instagram_comment", instagram_account_id=account.id)
    else:
        logger.error(
            "Public reply to comment %s failed: %s", comment.comment_id, public_response
        )

    # Meta allows exactly one private reply per comment — send it
    # regardless of whether the public reply above succeeded, since they're
    # independent Graph API calls and this is our only shot at the DM.
    private_response = await provider.send_private_reply(
        account.ig_business_id, comment.comment_id, reply["private_reply"]
    )
    if "error" not in private_response:
        comment.private_reply_sent = True
        usage.record_reply(db, account.user_id, "instagram_comment", instagram_account_id=account.id)
    else:
        logger.error(
            "Private reply to comment %s failed: %s", comment.comment_id, private_response
        )

    db.commit()
    return "Processed."
