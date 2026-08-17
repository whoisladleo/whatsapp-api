from sqlalchemy import func
from sqlalchemy.orm import Session
from app.db.models import Customer, Message, ConversationContext, ConversationState
from typing import Dict, Any, List, Optional


class ConversationNotFoundError(Exception):
    """No conversation with this phone number exists, or none of its history belongs to this account."""


class ConversationNotInHandoffError(Exception):
    """The conversation exists but isn't currently in HUMAN_HANDOFF state."""


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
    db: Session, account_id: int, limit: int = 100, customer_phone: Optional[str] = None
) -> List[Message]:
    query = db.query(Message).filter(Message.account_id == account_id)
    if customer_phone:
        customer_phone_expr = func.coalesce(Message.from_phone, Message.to_phone)
        query = query.filter(customer_phone_expr == customer_phone)
    return (
        query
        .order_by(Message.timestamp.desc())
        .limit(limit)
        .all()
    )


def update_message_status(db: Session, whatsapp_message_id: str, status: str):
    db.query(Message).filter(Message.whatsapp_message_id == whatsapp_message_id).update(
        {"status": status}
    )
    db.commit()


def resume_conversation_for_account(db: Session, account_id: int, phone: str) -> None:
    """
    Flips a HUMAN_HANDOFF conversation back to NEGOTIATING. Shared by the
    WhatsApp `/resume` merchant command (app/services/merchant_handler.py)
    and the HTTP endpoint (POST /whatsapp/handoffs/{phone}/resume) so the
    logic — including the account-scoping safeguard below — lives in one
    place. Raises rather than returning a message string so each caller can
    format the outcome for its own transport (WhatsApp reply text vs. an
    HTTP status code).

    NOTE: Customer/ConversationContext are global in this schema (not
    scoped per merchant/account). As a safeguard against one merchant
    resuming a conversation that actually belongs to a different merchant's
    customer, require that this customer has previously messaged *this*
    account before allowing the resume.
    """
    customer = db.query(Customer).filter(Customer.phone_number == phone).first()
    if not customer:
        raise ConversationNotFoundError(f"No conversation found for {phone}.")

    has_history_with_account = (
        db.query(Message)
        .filter(Message.from_phone == phone, Message.account_id == account_id)
        .first()
    )
    if not has_history_with_account:
        raise ConversationNotFoundError(f"No conversation with {phone} found for this account.")

    context = (
        db.query(ConversationContext)
        .filter(ConversationContext.customer_id == customer.id)
        .first()
    )
    if not context or context.state != ConversationState.HUMAN_HANDOFF:
        raise ConversationNotInHandoffError(f"{phone} is not currently in a human handoff state.")

    context.state = ConversationState.NEGOTIATING
    context.handoff_at = None
    db.commit()


def get_conversations_for_account(
    db: Session, account_id: int, exclude_phone: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Every distinct customer this account has exchanged messages with,
    ordered by most recent activity (either direction). A message row only
    ever has one of from_phone/to_phone set to the customer's number (the
    other is None — see create_message), so COALESCE picks it out regardless
    of direction.

    exclude_phone filters out the merchant's own number, so a merchant
    testing their account via /manage etc. doesn't show up as a customer
    conversation with themselves.
    """
    customer_phone = func.coalesce(Message.from_phone, Message.to_phone)

    query = (
        db.query(customer_phone.label("customer_phone"), func.max(Message.timestamp).label("last_activity_at"))
        .filter(Message.account_id == account_id, customer_phone.isnot(None))
        .group_by(customer_phone)
    )
    if exclude_phone:
        query = query.filter(customer_phone != exclude_phone)

    rows = query.order_by(func.max(Message.timestamp).desc()).all()

    results = []
    for row in rows:
        phone = row.customer_phone
        last_message = (
            db.query(Message)
            .filter(Message.account_id == account_id, customer_phone == phone)
            .order_by(Message.timestamp.desc())
            .first()
        )

        state = None
        customer = db.query(Customer).filter(Customer.phone_number == phone).first()
        if customer and customer.conversation:
            state = customer.conversation.state.value

        results.append(
            {
                "customer_phone": phone,
                "last_activity_at": row.last_activity_at,
                "last_message_body": last_message.body if last_message else None,
                "last_message_direction": last_message.direction if last_message else None,
                "state": state,
            }
        )

    return results


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
