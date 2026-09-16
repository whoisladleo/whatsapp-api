import logging

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def process_inbound_comment(account_id: int, comment_row_id: int):
    """
    Generates and sends Amara's reply to an inbound Instagram comment (the
    public reply and, where applicable, the comment-triggered private-reply
    DM). Stubbed for now — filled in once the AI prompt/reply generation
    piece is built. comment_row_id is InstagramComment.id, not Meta's own
    comment_id string.
    """
    logger.info(
        "Instagram comment row %s for account %s queued for processing (not yet implemented)",
        comment_row_id,
        account_id,
    )
