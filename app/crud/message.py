import uuid
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from app.db.models import Customer, Message, ConversationContext, ConversationState
from typing import Dict, Any, List, Optional


class ConversationNotFoundError(Exception):
    """No conversation with this phone number exists, or none of its history belongs to this account."""


class ConversationNotInHandoffError(Exception):
    """The conversation exists but isn't currently in HUMAN_HANDOFF state."""


def create_message(
    db: Session, message_data: Dict[str, Any], account_id: uuid.UUID
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
        attachments=message_data.get("attachments"),
        direction=message_data.get("direction"),
        status=message_data.get("status", "sent"),
        account_id=account_id,
    )
    db.add(db_message)
    try:
        db.commit()
    except IntegrityError:
        # Lost a concurrent insert race (two webhook deliveries for the
        # same message both missed the SELECT above); fall back to the
        # winner's row instead of 500ing (#86 pattern).
        db.rollback()
        if not whatsapp_message_id:
            raise
        existing = (
            db.query(Message)
            .filter(Message.whatsapp_message_id == whatsapp_message_id)
            .first()
        )
        if existing is None:
            raise
        return existing
    db.refresh(db_message)
    return db_message


def get_messages_by_account(
    db: Session, account_id: uuid.UUID, limit: int = 100, customer_phone: Optional[str] = None
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


def _prefer_scoped(conversations: List[ConversationContext], account_id: uuid.UUID) -> Optional[ConversationContext]:
    """This account's context out of a customer's rows: the scoped row when
    one exists, else the legacy NULL-account row (still unadopted)."""
    fallback = None
    for conversation in conversations or []:
        if conversation.account_id == account_id:
            return conversation
        if conversation.account_id is None and fallback is None:
            fallback = conversation
    return fallback


def get_context_for_account(
    db: Session, customer_id: uuid.UUID, account_id: uuid.UUID
) -> Optional[ConversationContext]:
    """Fetch-only version of get_or_create_context: the scoped row when one
    exists, else the legacy NULL-account row. Neither adopts nor creates."""
    return _prefer_scoped(
        db.query(ConversationContext)
        .filter(ConversationContext.customer_id == customer_id)
        .all(),
        account_id,
    )


def get_or_create_context(
    db: Session, customer_id: uuid.UUID, account_id: uuid.UUID
) -> ConversationContext:
    """
    Fetch-or-create the (customer, account) conversation context. A legacy
    NULL-account row is adopted by the first account that sees it (stamped
    in place, keeping its state — a handoff stays a handoff); otherwise a
    fresh NEGOTIATING row is created. Same lost-race fallback as
    crud_instagram.get_or_create_conversation (#86).
    """
    context = get_context_for_account(db, customer_id, account_id)
    if context is not None and context.account_id is not None:
        return context

    try:
        if context is not None:
            context.account_id = account_id
        else:
            context = ConversationContext(customer_id=customer_id, account_id=account_id)
            db.add(context)
        db.commit()
    except IntegrityError:
        db.rollback()
        context = (
            db.query(ConversationContext)
            .filter(
                ConversationContext.customer_id == customer_id,
                ConversationContext.account_id == account_id,
            )
            .first()
        )
        if context is None:
            raise
        return context
    db.refresh(context)
    return context


def resume_conversation_for_account(db: Session, account_id: uuid.UUID, phone: str) -> None:
    """
    Flips a HUMAN_HANDOFF conversation back to NEGOTIATING. Shared by the
    WhatsApp `/resume` merchant command (app/services/merchant_handler.py)
    and the HTTP endpoint (POST /whatsapp/handoffs/{phone}/resume) so the
    logic — including the account-scoping safeguard below — lives in one
    place. Raises rather than returning a message string so each caller can
    format the outcome for its own transport (WhatsApp reply text vs. an
    HTTP status code).

    The context lookup is scoped to this account: a handoff at another
    shop for the same phone is a different row and is left alone. The
    has-history check stays as well — it 404s (not 400s) when the phone
    never messaged this account at all, and it keeps a legacy NULL-account
    row attributable to the right shop on resume.
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

    context = get_context_for_account(db, customer.id, account_id)
    if not context or context.state != ConversationState.HUMAN_HANDOFF:
        raise ConversationNotInHandoffError(f"{phone} is not currently in a human handoff state.")

    if context.account_id is None:
        context.account_id = account_id
    context.state = ConversationState.NEGOTIATING
    context.handoff_at = None
    db.commit()


def get_conversations_for_account(
    db: Session,
    account_id: uuid.UUID,
    exclude_phone: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
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

    Runs in 3 queries regardless of page size: one grouped page of
    customer phones, one window-function fetch of each phone's latest
    message, and one bulk customer+state fetch.
    """
    customer_phone = func.coalesce(Message.from_phone, Message.to_phone)

    filters = [Message.account_id == account_id, customer_phone.isnot(None)]
    if exclude_phone:
        filters.append(customer_phone != exclude_phone)

    rows = (
        db.query(customer_phone.label("customer_phone"), func.max(Message.timestamp).label("last_activity_at"))
        .filter(*filters)
        .group_by(customer_phone)
        .order_by(func.max(Message.timestamp).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not rows:
        return []
    phones = [row.customer_phone for row in rows]

    # Latest message per phone in a single query: ROW_NUMBER partitions by
    # customer phone ordered by recency, then keep rank 1. Supported by both
    # Postgres (prod) and SQLite 3.25+ (tests).
    ranked = (
        db.query(
            Message.id.label("msg_id"),
            customer_phone.label("customer_phone"),
            func.row_number()
            .over(partition_by=customer_phone, order_by=Message.timestamp.desc())
            .label("rn"),
        )
        .filter(Message.account_id == account_id, customer_phone.in_(phones))
        .subquery()
    )
    last_messages = {
        phone: message
        for message, phone in (
            db.query(Message, ranked.c.customer_phone)
            .join(ranked, Message.id == ranked.c.msg_id)
            .filter(ranked.c.rn == 1)
            .all()
        )
    }

    customers = (
        db.query(Customer)
        .options(joinedload(Customer.conversations))
        .filter(Customer.phone_number.in_(phones))
        .all()
    )
    # Per-account state: the same phone can be mid-handoff at one shop and
    # negotiating at another — each account's list shows its own row.
    states = {}
    for customer in customers:
        context = _prefer_scoped(customer.conversations, account_id)
        states[customer.phone_number] = context.state.value if context else None

    return [
        {
            "customer_phone": row.customer_phone,
            "last_activity_at": row.last_activity_at,
            "last_message_body": last_messages[row.customer_phone].body
            if row.customer_phone in last_messages
            else None,
            "last_message_direction": last_messages[row.customer_phone].direction
            if row.customer_phone in last_messages
            else None,
            "state": states.get(row.customer_phone),
        }
        for row in rows
    ]


def get_message_stats_for_account(
    db: Session, account_id: uuid.UUID, exclude_phone: Optional[str] = None
) -> Dict[str, int]:
    """Total outbound messages sent vs. distinct customers messaged — same
    exclude_phone semantics as get_conversations_for_account, so the
    conversation count here matches what the Conversations page shows."""
    sent_query = db.query(Message).filter(
        Message.account_id == account_id, Message.direction == "outbound"
    )
    if exclude_phone:
        sent_query = sent_query.filter(
            func.coalesce(Message.from_phone, Message.to_phone) != exclude_phone
        )
    messages_sent = sent_query.count()

    customer_phone = func.coalesce(Message.from_phone, Message.to_phone)
    conversations_query = db.query(customer_phone).filter(
        Message.account_id == account_id, customer_phone.isnot(None)
    )
    if exclude_phone:
        conversations_query = conversations_query.filter(customer_phone != exclude_phone)
    conversations = conversations_query.distinct().count()

    return {"messages_sent": messages_sent, "conversations": conversations}


def get_recent_send_failures(
    db: Session, account_id: uuid.UUID, hours: int = 24
) -> Dict[str, Any]:
    """
    Outbound sends that failed at the WhatsApp API level in the last `hours`
    — e.g. Meta rejecting sends with "display name needs approval" (code
    131037), a state Meta's own phone-number status endpoint does NOT
    surface (verified_name/quality_rating/status can all look healthy while
    every send is actually failing). The real reason lives in raw_data.error
    (see app/tasks/webhook_processing.py's create_message call), but nothing
    previously exposed it outside of grepping worker logs.
    """
    since = datetime.utcnow() - timedelta(hours=hours)
    failures = (
        db.query(Message)
        .filter(
            Message.account_id == account_id,
            Message.direction == "outbound",
            Message.status == "failed",
            Message.timestamp >= since,
        )
        .order_by(Message.timestamp.desc())
        .all()
    )

    last_reason = None
    if failures:
        raw = failures[0].raw_data or {}
        last_reason = (raw.get("error") or {}).get("message")

    return {
        "count": len(failures),
        "last_failed_at": failures[0].timestamp if failures else None,
        "last_reason": last_reason,
    }


def get_pending_handoffs_for_account(db: Session, account_id: uuid.UUID) -> List[Dict[str, Any]]:
    """
    This account's customers currently in ConversationState.HUMAN_HANDOFF.
    Scoped by account first: candidate phones come from this account's own
    inbound messages, then each phone's (customer, account) context is
    checked — 3 queries total regardless of fleet size, instead of loading
    every handoff context plus one latest-inbound query each. Same response
    shape as before; last_message_body is now this account's latest inbound
    from that customer (previously the latest across all accounts).
    """
    phones = [
        row[0]
        for row in (
            db.query(Message.from_phone)
            .filter(
                Message.account_id == account_id,
                Message.direction == "inbound",
                Message.from_phone.isnot(None),
            )
            .distinct()
            .all()
        )
    ]
    if not phones:
        return []

    customers = (
        db.query(Customer)
        .options(joinedload(Customer.conversations))
        .filter(Customer.phone_number.in_(phones))
        .all()
    )
    contexts_by_phone = {
        customer.phone_number: _prefer_scoped(customer.conversations, account_id)
        for customer in customers
    }

    # Latest inbound per phone on this account in a single query — the same
    # ROW_NUMBER pattern get_conversations_for_account uses.
    ranked = (
        db.query(
            Message.id.label("msg_id"),
            Message.from_phone.label("customer_phone"),
            func.row_number()
            .over(partition_by=Message.from_phone, order_by=Message.timestamp.desc())
            .label("rn"),
        )
        .filter(
            Message.account_id == account_id,
            Message.direction == "inbound",
            Message.from_phone.in_(phones),
        )
        .subquery()
    )
    last_bodies = {
        phone: body
        for body, phone in (
            db.query(Message.body, ranked.c.customer_phone)
            .join(ranked, Message.id == ranked.c.msg_id)
            .filter(ranked.c.rn == 1)
            .all()
        )
    }

    results = []
    for phone in phones:
        context = contexts_by_phone.get(phone)
        if context is None or context.state != ConversationState.HUMAN_HANDOFF:
            continue
        results.append(
            {
                "customer_phone": phone,
                "handoff_at": context.handoff_at,
                "last_message_body": last_bodies.get(phone),
            }
        )

    return results
