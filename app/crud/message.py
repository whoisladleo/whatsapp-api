from sqlalchemy.orm import Session
from app.db.models import Message, ConversationContext, ConversationState
from typing import Dict, Any, List


def create_message(
    db: Session, message_data: Dict[str, Any], account_id: int
) -> Message:
    whatsapp_message_id = message_data.get("whatsapp_message_id")
    
    if whatsapp_message_id:
        existing = db.query(Message).filter(Message.whatsapp_message_id == whatsapp_message_id).first()
        if existing:
            return existing

    db_message = Message(
        whatsapp_message_id=whatsapp_message_id,
        from_phone=message_data.get("from_phone"),
        to_phone=message_data.get("to_phone"),
        type=message_data.get("type", "text"),
        body=message_data.get("body"),
        raw_data=message_data.get("raw"),
        direction=message_data.get("direction"),
        status=message_data.get("status", "sent"),
        account_id=account_id,
    )
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message


def get_messages_by_account(
    db: Session, account_id: int, limit: int = 100
) -> List[Message]:
    return (
        db.query(Message)
        .filter(Message.account_id == account_id)
        .order_by(Message.timestamp.desc())
        .limit(limit)
        .all()
    )


def update_message_status(db: Session, whatsapp_message_id: str, status: str):
    db.query(Message).filter(Message.whatsapp_message_id == whatsapp_message_id).update(
        {"status": status}
    )
    db.commit()


def get_pending_handoffs_for_account(db: Session, account_id: int) -> List[Dict[str, Any]]:
    """
    Customers currently in ConversationState.HUMAN_HANDOFF whose most recent
    inbound message belongs to this account. There is no direct
    Customer/ConversationContext -> WhatsAppAccount link in the schema, so
    the owning account is inferred from the latest inbound message, matching
    the same inference used to route the handoff reminder task.
    """
    contexts = (
        db.query(ConversationContext)
        .filter(ConversationContext.state == ConversationState.HUMAN_HANDOFF)
        .all()
    )

    results = []
    for context in contexts:
        customer = context.customer
        if not customer:
            continue

        last_inbound = (
            db.query(Message)
            .filter(Message.from_phone == customer.phone_number, Message.direction == "inbound")
            .order_by(Message.timestamp.desc())
            .first()
        )
        if not last_inbound or last_inbound.account_id != account_id:
            continue

        results.append(
            {
                "customer_phone": customer.phone_number,
                "handoff_at": context.handoff_at,
                "last_message_body": last_inbound.body,
            }
        )

    return results
