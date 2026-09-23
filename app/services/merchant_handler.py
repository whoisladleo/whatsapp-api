from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount, Product, Message, InstagramAccount, InstagramCustomer
from app.crud import message as crud_message
from app.crud import instagram as crud_instagram
import re

async def handle_merchant_message(db: Session, account: WhatsAppAccount, message_body: str, from_phone: str) -> str:
    """
    Handles messages from the merchant.
    Commands:
    /stock [Product_Name] +[Qty]
    /out_of_stock [Product_Name]
    /price [Product_Name] max:[Price] min:[Price]
    /resume [Phone]
    /reply [Phone|@instagram_username] [Message]
    """
    body = message_body.strip()

    # If the merchant just types /stock or /price without arguments, they enter MANAGEMENT state
    if body.lower() in ["/stock", "/price", "/manage"]:
        return (
            "You are in management mode.\n\n"
            "Examples:\n"
            "- To update stock: `/stock Shoes +10`\n"
            "- To set out of stock: `/out_of_stock Shoes`\n"
            "- To update price: `/price Shoes max:50000 min:40000`\n"
            "- To hand a conversation back to the AI: `/resume 2557XXXXXXXX`\n"
            "- To reply to a customer yourself: `/reply 2557XXXXXXXX Your message` or "
            "`/reply @their_instagram_username Your message`"
        )

    if body.lower() == "/resume" or body.lower().startswith("/resume "):
        phone = body[len("/resume"):].strip()
        if not phone:
            return "Usage: `/resume 2557XXXXXXXX`"

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
            return await _relay_to_instagram_customer(db, account, identifier[1:], reply_text)
        return await _relay_to_whatsapp_customer(db, account, identifier, reply_text)

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
            return f"Updated prices for '{product.name}'. Max: {max_p}, Min: {min_p}"
        else:
            return "Invalid format. Example: `/price Shoes max:50000 min:40000`"

    return "Unknown command. Send `/manage` to see available commands."


async def _relay_to_whatsapp_customer(db: Session, account: WhatsAppAccount, phone: str, reply_text: str) -> str:
    """
    Sends `reply_text` as the merchant's own manual reply to a WhatsApp
    customer, via this same account's credentials — the WhatsApp-native
    counterpart of the dashboard's POST /whatsapp/messages/send. Requires
    prior message history with *this* account (Customer is global, not
    merchant-scoped, in this schema — same safeguard as /resume) rather
    than trusting an arbitrary typed phone number outright, since a raw
    text command is far more typo-prone than a dashboard form.
    """
    from app.core.provider import WhatsAppProvider

    has_history = (
        db.query(Message)
        .filter(Message.from_phone == phone, Message.account_id == account.id)
        .first()
    )
    if not has_history:
        return f"No conversation with {phone} found for this account."

    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = await provider.send_text(phone, reply_text)
    except Exception as e:
        return f"Failed to send reply to {phone}: {e}"

    if not resp or "messages" not in resp:
        error_detail = resp.get("error", {}).get("message", "unknown error") if resp else "unknown error"
        return f"Failed to send reply to {phone}: {error_detail}"

    crud_message.create_message(
        db,
        {
            "whatsapp_message_id": resp["messages"][0].get("id"),
            "to_phone": phone,
            "type": "text",
            "body": reply_text,
            "direction": "outbound",
            "status": "sent",
            "raw": resp,
        },
        account.id,
    )
    return f"Sent to {phone}."


async def _relay_to_instagram_customer(db: Session, account: WhatsAppAccount, ig_username: str, reply_text: str) -> str:
    """
    Lets a merchant reply to an Instagram DM customer from their WhatsApp
    number — the whole point being that this WhatsApp account is how the
    merchant talks *to* Amara/Taskflow, even for a conversation actually
    happening on a different channel (see CLAUDE.md's Instagram section on
    _notify_merchant_of_instagram_handoff, the read side of this same
    cross-channel relay). Scoped to this merchant's own connected
    InstagramAccount and an InstagramCustomer who has actually messaged it
    — InstagramCustomer.account_id already scopes ownership directly, no
    global-Customer ambiguity the way WhatsApp's ambiguity check needs.
    """
    from app.core.instagram_provider import InstagramProvider

    ig_account = (
        db.query(InstagramAccount)
        .filter(InstagramAccount.user_id == account.user_id, InstagramAccount.access_token != "")
        .first()
    )
    if not ig_account:
        return "No Instagram account connected."

    ig_customer = (
        db.query(InstagramCustomer)
        .filter(InstagramCustomer.account_id == ig_account.id, InstagramCustomer.username == ig_username)
        .first()
    )
    if not ig_customer:
        return f"No Instagram conversation with @{ig_username} found."

    provider = InstagramProvider(ig_account.access_token)
    try:
        resp = await provider.send_direct_message(ig_account.ig_business_id, ig_customer.ig_scoped_id, reply_text)
    except Exception as e:
        return f"Failed to send reply to @{ig_username}: {e}"

    if "error" in resp:
        return f"Failed to send reply to @{ig_username}: {resp['error'].get('message', 'unknown error')}"

    crud_instagram.create_direct_message(
        db,
        account_id=ig_account.id,
        customer_id=ig_customer.id,
        direction="outbound",
        body=reply_text,
        ig_message_id=resp.get("message_id"),
    )
    return f"Sent to @{ig_username}."
