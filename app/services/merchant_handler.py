import logging
import re
import uuid

from sqlalchemy.orm import Session
from app.core.attachments import describe_attachments
from app.core.instagram_provider import InstagramProvider
from app.core.provider import WhatsAppProvider
from app.core.security import normalize_phone
from app.db.models import (
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramCustomer,
    WhatsAppAccount,
    Product,
    Message,
    Order,
    OrderStatus,
    PaymentType,
)
from app.crud import message as crud_message
from app.crud import instagram as crud_instagram
from app.services import usage
from app.services.handoff_flow import (
    list_pending_handoffs,
    relay_to_instagram_customer,
    relay_to_whatsapp_customer,
    resolve_instagram_customer,
    set_directive_ig,
    set_directive_wa,
)
from app.services.payment_resend import ResendOutcome, merchant_resend_order

logger = logging.getLogger(__name__)

async def handle_merchant_message(db: Session, account: WhatsAppAccount, message_body: str, from_phone: str) -> str:
    """
    Handles messages from the merchant.
    Commands:
    /stock [Product_Name] +[Qty]
    /out_of_stock [Product_Name]
    /price [Product_Name] max:[Price] min:[Price]
    /resume [Phone|@instagram_username]
    /reply [Phone|@instagram_username] [Message]
    /resend [Phone|@instagram_username]
    /direct [Phone|@instagram_username|pending-#] [Instruction]
    /cancel (leave an armed handoff reply-mode without sending)
    """
    body = message_body.strip()

    if body.lower() == "/cancel":
        from app.services.handoff_flow import clear_reply_mode, get_reply_mode

        if get_reply_mode(account.user_id) is None:
            return "Nothing to cancel."
        clear_reply_mode(account.user_id)
        return "Cancelled — nothing was sent."

    # If the merchant just types /stock or /price without arguments, they enter MANAGEMENT state
    if body.lower() in ["/stock", "/price", "/manage"]:
        return (
            "You are in management mode.\n\n"
            "Examples:\n"
            "- To update stock: `/stock Shoes +10`\n"
            "- To set out of stock: `/out_of_stock Shoes`\n"
            "- To update price: `/price Shoes max:50000 min:40000`\n"
            "- To hand a conversation back to the AI: `/resume 2557XXXXXXXX` or "
            "`/resume @their_instagram_username`\n"
            "- To reply to a customer yourself: `/reply 2557XXXXXXXX Your message` or "
            "`/reply @their_instagram_username Your message`\n"
            "- To re-fire a dead payment prompt: `/resend 2557XXXXXXXX` or "
            "`/resend @their_instagram_username`\n"
            "- To make Amara message a customer right now: `/direct 2557XXXXXXXX Your instruction` or "
            "`/direct @their_instagram_username Your instruction`"
        )

    if body.lower() == "/resume" or body.lower().startswith("/resume "):
        identifier = body[len("/resume"):].strip()
        if not identifier:
            return _pending_pick_list(db, account, "resume")

        pick = _pending_pick(db, account, identifier)
        if pick is not None:
            return await _resume_pending_pick(db, account, pick)

        # Same @ disambiguation /reply uses below — a real phone number
        # never starts with @, so this is unambiguous.
        if identifier.startswith("@"):
            return await _resume_instagram_conversation(db, account, identifier[1:])

        phone = identifier
        try:
            crud_message.resume_conversation_for_account(db, account.id, phone)
        except crud_message.ConversationNotFoundError as e:
            return str(e)
        except crud_message.ConversationNotInHandoffError as e:
            return str(e)
        return f"Conversation with {phone} resumed. Amara will respond to their next message."

    if body.lower() == "/reply" or body.lower().startswith("/reply "):
        rest = body[len("/reply"):].strip()
        match = re.match(r"(\S+)\s+(.+)", rest, re.DOTALL)
        if not match:
            return (
                "Usage: `/reply 2557XXXXXXXX Your message` (WhatsApp customer) or "
                "`/reply @their_instagram_username Your message` (Instagram customer)"
            )
        identifier, reply_text = match.group(1), match.group(2).strip()

        if identifier.startswith("@"):
            return await relay_to_instagram_customer(db, account.owner, identifier[1:], reply_text)
        return await relay_to_whatsapp_customer(db, account, identifier, reply_text)

    if body.lower() == "/direct" or body.lower().startswith("/direct "):
        rest = body[len("/direct"):].strip()
        match = re.match(r"(\S+)\s+(.+)", rest, re.DOTALL)
        if not match or not match.group(2).strip():
            return (
                "Usage: `/direct 2557XXXXXXXX Your instruction` (WhatsApp customer), "
                "`/direct @their_instagram_username Your instruction` (Instagram customer), "
                "or `/direct <number from the waiting list> Your instruction`"
            )
        identifier, instruction = match.group(1), match.group(2).strip()

        pick = _pending_pick(db, account, identifier)
        if pick is not None:
            return await _direct_pending_pick(db, account, pick, instruction)

        if identifier.startswith("@"):
            return await _direct_instagram_customer(db, account, identifier[1:], instruction)
        return await _direct_whatsapp_customer(db, account, identifier, instruction)

    if body.lower() == "/resend" or body.lower().startswith("/resend "):
        identifier = body[len("/resend"):].strip()
        if not identifier:
            return _pending_pick_list(db, account, "resend")

        pick = _pending_pick(db, account, identifier)
        if pick is not None:
            return await _resend_pending_pick(db, account, pick)

        if identifier.startswith("@"):
            return await _resend_instagram_customer(db, account, identifier[1:])
        return await _resend_whatsapp_customer(db, account, identifier)

    if body.lower().startswith("/stock "):
        # Parse: /stock [Product_Name] +[Qty] or -[Qty]
        match = re.match(r"/stock\s+(.+)\s+([+-]\d+)", body, re.IGNORECASE)
        if match:
            product_name = match.group(1).strip()
            qty_change = int(match.group(2))

            # Find product
            product = db.query(Product).filter(
                Product.merchant_id == account.user_id,
                Product.name.ilike(product_name)
            ).first()

            if not product:
                return f"Product '{product_name}' not found."

            # quantity is nullable (legacy rows) and a decrement past
            # zero would leave phantom negative stock — clamp at 0.
            product.quantity = max(0, (product.quantity or 0) + qty_change)
            db.commit()
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(product.id, "upsert")
            return f"Updated stock for '{product.name}'. New quantity: {product.quantity}"
        else:
            return "Invalid format. Example: `/stock Shoes +10`"

    elif body.lower().startswith("/out_of_stock "):
        match = re.match(r"/out_of_stock\s+(.+)", body, re.IGNORECASE)
        if match:
            product_name = match.group(1).strip()
            product = db.query(Product).filter(
                Product.merchant_id == account.user_id,
                Product.name.ilike(product_name)
            ).first()

            if not product:
                return f"Product '{product_name}' not found."

            product.quantity = 0
            db.commit()
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(product.id, "upsert")
            return f"'{product.name}' is now out of stock."

    elif body.lower().startswith("/price "):
        match = re.match(r"/price\s+(.+)\s+max:(\d+)\s+min:(\d+)", body, re.IGNORECASE)
        if match:
            product_name = match.group(1).strip()
            max_p = float(match.group(2))
            min_p = float(match.group(3))
            if not 0 < min_p <= max_p:
                return "Invalid prices: need 0 < min <= max. Example: `/price Shoes max:50000 min:40000`"

            product = db.query(Product).filter(
                Product.merchant_id == account.user_id,
                Product.name.ilike(product_name)
            ).first()

            if not product:
                return f"Product '{product_name}' not found."

            product.max_price = max_p
            product.min_price = min_p
            db.commit()
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(product.id, "upsert")
            return f"Updated prices for '{product.name}'. Max: {max_p}, Min: {min_p}"
        else:
            return "Invalid format. Example: `/price Shoes max:50000 min:40000`"

    return "Unknown command. Send `/manage` to see available commands."


def _pending_pick_list(db: Session, account: WhatsAppAccount, command: str) -> str:
    """Bare /resume and /resend answer with the merchant's waiting
    handoffs — numbered, so a tap-sized `/resume 1` picks one — instead
    of bare usage text nobody can act on without the phone numbers."""
    pending = list_pending_handoffs(db, account.owner) if account.owner else []
    if not pending:
        return "No customers are waiting for you right now."
    lines = [f"{i}. {entry['name']}" for i, entry in enumerate(pending, 1)]
    return (
        "Customers waiting for you:\n"
        + "\n".join(lines)
        + f"\nReply with /{command} <number above>, or /{command} <phone|@username>."
    )


def _pending_pick(db: Session, account: WhatsAppAccount, identifier: str):
    """Resolves a bare-list index ('1') to its pending entry. A real
    phone ref is far too long to collide with a list position, so a
    small all-digit identifier is unambiguous — anything else (or an
    out-of-range index) falls through to the normal with-args path."""
    if not identifier.isdigit():
        return None
    pending = list_pending_handoffs(db, account.owner) if account.owner else []
    idx = int(identifier) - 1
    if not 0 <= idx < len(pending):
        return None
    return pending[idx]


async def _resume_pending_pick(db: Session, account: WhatsAppAccount, pick: dict) -> str:
    # A pick carries its own account context (the merchant may run
    # several business numbers) — resume against the entry's account,
    # not whichever number the command arrived on.
    if pick["channel"] == "instagram":
        return await _resume_instagram_conversation(db, account, pick["ref"])
    try:
        crud_message.resume_conversation_for_account(db, uuid.UUID(pick["account_id"]), pick["ref"])
    except crud_message.ConversationNotFoundError as e:
        return str(e)
    except crud_message.ConversationNotInHandoffError as e:
        return str(e)
    return f"Conversation with {pick['ref']} resumed. Amara will respond to their next message."


async def _resend_pending_pick(db: Session, account: WhatsAppAccount, pick: dict) -> str:
    if pick["channel"] == "instagram":
        return await _resend_instagram_customer(db, account, pick["ref"])
    pick_account = (
        db.query(WhatsAppAccount).filter(WhatsAppAccount.id == uuid.UUID(pick["account_id"])).first()
    )
    if not pick_account:
        return "That business number is no longer connected."
    return await _resend_whatsapp_customer(db, pick_account, pick["ref"])


async def _direct_pending_pick(
    db: Session, account: WhatsAppAccount, pick: dict, instruction: str
) -> str:
    # Same pick shape as _resume/_resend above — the entry carries its own
    # account context, and the directive is keyed to it, not to whichever
    # number the command arrived on.
    if pick["channel"] == "instagram":
        if not set_directive_ig(uuid.UUID(pick["customer_id"]), instruction):
            return "Couldn't save that instruction — please try again."
        ig_customer = (
            db.query(InstagramCustomer)
            .filter(InstagramCustomer.id == uuid.UUID(pick["customer_id"]))
            .first()
        )
        # Instagram picks carry account_id=None — the account comes from
        # the customer row instead.
        ig_account = (
            db.query(InstagramAccount)
            .filter(InstagramAccount.id == ig_customer.account_id)
            .first()
            if ig_customer
            else None
        )
        if not ig_account or not ig_customer:
            return "That Instagram conversation is no longer connected."
        who = f"@{ig_customer.username or ig_customer.ig_scoped_id}"
        return await _send_direct_now_ig(db, ig_account, ig_customer, who, instruction)
    pick_account = (
        db.query(WhatsAppAccount).filter(WhatsAppAccount.id == uuid.UUID(pick["account_id"])).first()
    )
    if not pick_account:
        return "That business number is no longer connected."
    return await _direct_whatsapp_customer(db, pick_account, pick["ref"], instruction)


async def _direct_whatsapp_customer(
    db: Session, account: WhatsAppAccount, phone: str, instruction: str
) -> str:
    """
    `/direct 2557XXXXXXXX <instruction>`: arms a one-shot hidden steering
    instruction and has Amara send it right now. Requires prior message
    history with this account, like /reply's relay — a typo'd number must
    not arm a phantom directive.
    """
    normalized = normalize_phone(phone)
    customer = _find_customer_by_phone(db, normalized, phone)
    has_history = (
        db.query(Message)
        .filter(Message.account_id == account.id, Message.from_phone.in_([phone, normalized]))
        .first()
        if customer
        else None
    )
    if not customer or not has_history:
        return f"No conversation with {normalized} found for this account."
    if not set_directive_wa(account.id, customer.id, instruction):
        return "Couldn't save that instruction — please try again."
    return await _send_direct_now_wa(db, account, customer, normalized, instruction)


async def _send_direct_now_wa(
    db: Session,
    account: WhatsAppAccount,
    customer: Customer,
    phone_display: str,
    instruction: str,
) -> str:
    """
    Instant half of /direct (WhatsApp): generates Amara's steered reply
    now and sends it, instead of waiting for the customer's next message.
    Anything that prevents a send (handoff, quota, outage, Meta rejection)
    keeps the already-armed directive queued for her next generated reply
    and says so — never silently dropped.
    """
    from app.services.llm import llm_service

    if llm_service is None or llm_service.client is None:
        return (
            f"Amara is unavailable right now — kept queued; her next reply to "
            f"{phone_display} will follow this instruction: {instruction}"
        )
    holding = usage.check_reply_quota(db, account.owner) if account.owner else None
    if holding is not None:
        return (
            f"Reply allowance exhausted — kept queued for {phone_display}; it will "
            f"apply once replies renew: {instruction}"
        )
    context = crud_message.get_or_create_context(db, customer.id, account.id)
    if context.state == ConversationState.HUMAN_HANDOFF:
        # generate_response would return None without consuming — the
        # directive is still armed.
        return (
            f"That conversation is handed off to you — /resume it first; kept "
            f"queued for {phone_display}: {instruction}"
        )
    try:
        response_text = await llm_service.generate_response(
            message_body="",
            product_slug=account.product_slug,
            system_prompt=account.system_prompt,
            from_phone=customer.phone_number,
            db=db,
            account=account,
            proactive=True,
        )
    except Exception as e:
        logger.error(f"Instant /direct generation failed for {phone_display}: {e}")
        set_directive_wa(account.id, customer.id, instruction)
        return (
            f"Couldn't generate Amara's message just now — kept queued for "
            f"{phone_display}: {instruction}"
        )
    if not response_text:
        # Proactive generation failed (outage/empty/cutoff returns None
        # without handing off) after consuming the directive — re-arm it.
        set_directive_wa(account.id, customer.id, instruction)
        return (
            f"Amara's having trouble right now — kept queued for {phone_display}, "
            f"try again in a bit: {instruction}"
        )
    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(customer.phone_number, response_text)
    except Exception as e:
        logger.error(f"Instant /direct send failed for {phone_display}: {e}")
        resp = None
    crud_message.create_message(
        db,
        {
            "to_phone": customer.phone_number,
            "type": "text",
            "body": response_text,
            "direction": "outbound",
            "status": "sent" if resp and "messages" in resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )
    if resp and "messages" in resp:
        usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)
        return f"Sent to {phone_display}: {response_text}"
    set_directive_wa(account.id, customer.id, instruction)
    return (
        f"Couldn't deliver to {phone_display} just now (Meta rejected it — likely "
        f"outside the 24h window). Kept queued for Amara's next reply."
    )


def _find_customer_by_phone(db: Session, normalized: str, raw: str):
    """Customer lookup tolerant of legacy non-normalized phone rows."""
    customer = db.query(Customer).filter(Customer.phone_number == normalized).first()
    if customer is None and raw != normalized:
        customer = db.query(Customer).filter(Customer.phone_number == raw).first()
    return customer


async def _direct_instagram_customer(
    db: Session, account: WhatsAppAccount, ig_username: str, instruction: str
) -> str:
    """
    `/direct @username <instruction>` — the Instagram counterpart, scoped
    the same way /reply's own @ handling is (this merchant's connected
    InstagramAccount, username or ig_scoped_id).
    """
    ig_account, ig_customer = resolve_instagram_customer(db, account.user_id, ig_username)
    if not ig_account:
        return "No Instagram account connected."
    if not ig_customer:
        return f"No Instagram conversation with @{ig_username} found."
    if not set_directive_ig(ig_customer.id, instruction):
        return "Couldn't save that instruction — please try again."
    who = f"@{ig_customer.username or ig_customer.ig_scoped_id}"
    return await _send_direct_now_ig(db, ig_account, ig_customer, who, instruction)


async def _send_direct_now_ig(
    db: Session,
    ig_account: InstagramAccount,
    ig_customer: InstagramCustomer,
    who: str,
    instruction: str,
) -> str:
    """
    Instant half of /direct (Instagram): the counterpart to
    _send_direct_now_wa — same arm-then-send contract, same keep-queued
    fallbacks. The handoff check lives here because the IG generator (unlike
    the WhatsApp one) has no handoff early-return of its own.
    """
    from app.services.llm import llm_service

    if llm_service is None or llm_service.client is None:
        return (
            f"Amara is unavailable right now — kept queued; her next reply to "
            f"{who} will follow this instruction: {instruction}"
        )
    context = crud_instagram.get_or_create_conversation(db, ig_customer.id)
    if context.state == ConversationState.HUMAN_HANDOFF:
        return (
            f"That conversation is handed off to you — /resume it first; kept "
            f"queued for {who}: {instruction}"
        )
    holding = usage.check_reply_quota(db, ig_account.owner) if ig_account.owner else None
    if holding is not None:
        return (
            f"Reply allowance exhausted — kept queued for {who}; it will "
            f"apply once replies renew: {instruction}"
        )
    history = [
        {"direction": m.direction, "body": m.body or describe_attachments(m.attachments)}
        for m in crud_instagram.get_dm_history(db, ig_customer.id, limit=20)
    ]
    try:
        response_text = await llm_service.generate_dm_response(
            message_body="",
            db=db,
            account=ig_account,
            customer=ig_customer,
            context=context,
            conversation_history=history,
            account_name=ig_account.username or "the shop",
            proactive=True,
        )
    except Exception as e:
        logger.error(f"Instant /direct generation failed for {who}: {e}")
        set_directive_ig(ig_customer.id, instruction)
        return f"Couldn't generate Amara's message just now — kept queued for {who}: {instruction}"
    if not response_text:
        # Proactive generation failed (outage/empty/cutoff returns None
        # without handing off) after consuming the directive — re-arm it.
        # (No-client None can't reach here — the pre-check above returns
        # first — but the re-arm is harmless either way.)
        set_directive_ig(ig_customer.id, instruction)
        return (
            f"Amara's having trouble right now — kept queued for {who}, "
            f"try again in a bit: {instruction}"
        )
    provider = InstagramProvider(ig_account.access_token)
    try:
        resp = await provider.send_direct_message(
            ig_account.ig_business_id, ig_customer.ig_scoped_id, response_text
        )
    except Exception as e:
        logger.error(f"Instant /direct send failed for {who}: {e}")
        resp = {"error": str(e)}
    crud_instagram.create_direct_message(
        db,
        account_id=ig_account.id,
        customer_id=ig_customer.id,
        direction="outbound",
        body=response_text,
        ig_message_id=(resp or {}).get("message_id"),
    )
    if resp and "error" not in resp:
        usage.record_reply(db, ig_account.user_id, "instagram_dm", instagram_account_id=ig_account.id)
        return f"Sent to {who}: {response_text}"
    set_directive_ig(ig_customer.id, instruction)
    return (
        f"Couldn't deliver to {who} just now (Meta rejected it — likely "
        f"outside the 24h window). Kept queued for Amara's next reply."
    )


def _render_resend_result(who: str, order: Order, result) -> str:
    """Merchant-facing rendering of a ResendResult — always names the
    customer, product, and price so a re-fired prompt is never a mystery
    charge. result.detail carries the pre-written safe explanation for
    every refusal outcome."""
    product_name = order.product.name if order.product else "their order"
    price = f"TZS {(order.agreed_price or 0):,.0f}"
    outcome = result.outcome
    if outcome is ResendOutcome.RESENT:
        return f"Payment prompt re-sent to {who} for {product_name} ({price})."
    if outcome is ResendOutcome.FRESH_ORDERED:
        return (
            f"The old hold had expired — started a fresh order for {product_name} ({price}) "
            f"and sent the prompt to {who}."
        )
    if outcome is ResendOutcome.THROTTLED:
        return f"A prompt already went out recently — try again in {result.retry_after_seconds or 60}s."
    return result.detail or "This order can't be resent right now."


def _pending_full_orders(query):
    return (
        query.filter(Order.status == OrderStatus.PENDING, Order.payment_type == PaymentType.FULL)
        .order_by(Order.created_at.desc())
        .all()
    )


def _ambiguous_resend_reply(candidates) -> str:
    lines = [
        f"- {o.product.name if o.product else 'Order'} — TZS {(o.agreed_price or 0):,.0f}"
        for o in candidates
    ]
    return (
        "This customer has more than one pending order, so I won't guess which prompt to re-fire:\n"
        + "\n".join(lines)
        + "\nUse the resend button on the exact order in the dashboard."
    )


async def _resend_whatsapp_customer(db: Session, account: WhatsAppAccount, phone: str) -> str:
    """
    `/resend 2557XXXXXXXX`: re-fires the Tunzaa prompt on the customer's
    single pending FULL order for this account (expired holds re-close
    from identical terms — see payment_resend.merchant_resend_order).
    Refuses rather than guesses when several are live, and explains
    when only an installment plan is pending (Tunzaa pushes those legs
    itself). Phone-keyed customers are global, but the order lookup is
    scoped to this account — another shop's orders never leak in.
    """
    normalized = normalize_phone(phone)
    customer = db.query(Customer).filter(Customer.phone_number == normalized).first()
    base = (
        db.query(Order).filter(Order.account_id == account.id, Order.customer_id == customer.id)
        if customer
        else db.query(Order).filter(False)
    )
    candidates = _pending_full_orders(base)
    if len(candidates) > 1:
        return _ambiguous_resend_reply(candidates)
    if len(candidates) == 1:
        return _render_resend_result(normalized, candidates[0], await merchant_resend_order(db, candidates[0]))
    has_installment = (
        base.filter(Order.status == OrderStatus.PENDING, Order.payment_type == PaymentType.INSTALLMENT).first()
        is not None
    )
    if has_installment:
        return "This customer's pending order is an installment plan — prompts go out on Tunzaa's own schedule, there is nothing to resend."
    return f"No pending orders for {normalized}."


async def _resend_instagram_customer(db: Session, account: WhatsAppAccount, ig_username: str) -> str:
    """
    `/resend @username` — the Instagram counterpart, scoped the same way
    _resume_instagram_conversation is (this merchant's connected
    InstagramAccount, username or ig_scoped_id). Same single-order rule
    as the WhatsApp path above.
    """
    ig_account, ig_customer = resolve_instagram_customer(db, account.user_id, ig_username)
    if not ig_account:
        return "No Instagram account connected."
    if not ig_customer:
        return f"No Instagram conversation with @{ig_username} found."

    who = f"@{ig_customer.username or ig_customer.ig_scoped_id}"
    base = db.query(Order).filter(
        Order.instagram_account_id == ig_account.id,
        Order.instagram_customer_id == ig_customer.id,
    )
    candidates = _pending_full_orders(base)
    if len(candidates) > 1:
        return _ambiguous_resend_reply(candidates)
    if len(candidates) == 1:
        return _render_resend_result(who, candidates[0], await merchant_resend_order(db, candidates[0]))
    has_installment = (
        base.filter(Order.status == OrderStatus.PENDING, Order.payment_type == PaymentType.INSTALLMENT).first()
        is not None
    )
    if has_installment:
        return "This customer's pending order is an installment plan — prompts go out on Tunzaa's own schedule, there is nothing to resend."
    return f"No pending orders for {who}."


async def _resume_instagram_conversation(db: Session, account: WhatsAppAccount, ig_username: str) -> str:
    """
    `/resume @username` — the Instagram counterpart to the WhatsApp phone
    path above, and to `/reply`'s own @ handling. Scoped the same way
    `_relay_to_instagram_customer` is (this merchant's connected
    InstagramAccount, matching on username or ig_scoped_id as a fallback
    for a customer whose display-name backfill hasn't run yet) — no
    global-Customer ambiguity to guard against here, since
    InstagramCustomer.account_id already scopes ownership directly.
    """
    ig_account, ig_customer = resolve_instagram_customer(db, account.user_id, ig_username)
    if not ig_account:
        return "No Instagram account connected."
    if not ig_customer:
        return f"No Instagram conversation with @{ig_username} found."

    try:
        crud_instagram.resume_conversation(db, ig_customer.id)
    except crud_instagram.ConversationNotInHandoffError:
        # resume_conversation's own error message is keyed by the opaque
        # customer UUID, not something to show the merchant — build our
        # own using what they actually typed instead.
        return f"@{ig_username} is not currently in a human handoff state."
    return f"Conversation with @{ig_username} resumed. Amara will respond to their next message."
