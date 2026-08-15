from app.crud import message as crud_message
from app.db.models import ConversationContext, ConversationState


def test_get_pending_handoffs_skips_orphaned_context(db_session):
    # A context row with no matching customer row is skipped rather than
    # raising, since context.customer resolves to None.
    orphan = ConversationContext(customer_id=999999, state=ConversationState.HUMAN_HANDOFF)
    db_session.add(orphan)
    db_session.commit()

    result = crud_message.get_pending_handoffs_for_account(db_session, account_id=1)

    assert result == []
