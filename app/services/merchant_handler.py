from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount, Product, Message, InstagramAccount, InstagramCustomer
from app.crud import message as crud_message
from app.crud import instagram as crud_instagram
from app.services.handoff_flow import relay_to_instagram_customer, relay_to_whatsapp_customer
import re

async def handle_merchant_message(db: Session, account: WhatsAppAccount, message_body: str, from_phone: str) -> str:
    """
    Handles messages from the merchant.
    Commands:
    /stock [Product_Name] +[Qty]
    /out_of_stock [Product_Name]
    /price [Product_Name] max:[Price] min:[Price]
    /resume [Phone|@instagram_username]
    /reply [Phone|@instagram_username] [Message]
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
            "`/reply @their_instagram_username Your message`"
        )

    if body.lower() == "/resume" or body.lower().startswith("/resume "):
        identifier = body[len("/resume"):].strip()
        if not identifier:
            return "Usage: `/resume 2557XXXXXXXX` (WhatsApp customer) or `/resume @their_instagram_username` (Instagram customer)"

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

            product.quantity += qty_change
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
    ig_account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == account.user_id, InstagramAccount.access_token != "")
        .first()
    )
    if not ig_account:
        return "No Instagram account connected."

    ig_customer = (
        db.query(InstagramCustomer)
        .filter(
            InstagramCustomer.account_id == ig_account.id,
            (InstagramCustomer.username == ig_username) | (InstagramCustomer.ig_scoped_id == ig_username),
        )
        .first()
    )
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
