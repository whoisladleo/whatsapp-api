from sqlalchemy.orm import Session
from app.db.models import Message
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
