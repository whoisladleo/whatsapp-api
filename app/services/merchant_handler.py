from sqlalchemy.orm import Session
from app.db.models import WhatsAppAccount, Product, ConversationContext, ConversationState, Customer, Message
import re

async def handle_merchant_message(db: Session, account: WhatsAppAccount, message_body: str, from_phone: str) -> str:
    """
    Handles messages from the merchant.
    Commands:
    /stock [Product_Name] +[Qty]
    /out_of_stock [Product_Name]
    /price [Product_Name] max:[Price] min:[Price]
    /resume [Phone]
    """
    body = message_body.strip()

    # If the merchant just types /stock or /price without arguments, they enter MANAGEMENT state
    if body.lower() in ["/stock", "/price", "/manage"]:
        return "You are in management mode.\n\nExamples:\n- To update stock: `/stock Shoes +10`\n- To set out of stock: `/out_of_stock Shoes`\n- To update price: `/price Shoes max:50000 min:40000`\n- To hand a conversation back to the AI: `/resume 2557XXXXXXXX`"

    if body.lower() == "/resume" or body.lower().startswith("/resume "):
        phone = body[len("/resume"):].strip()
        if not phone:
            return "Usage: `/resume 2557XXXXXXXX`"

        customer = db.query(Customer).filter(Customer.phone_number == phone).first()
        if not customer:
            return f"No conversation found for {phone}."

        # NOTE: Customer/ConversationContext are global in this schema (not
        # scoped per merchant/account). As a safeguard against one merchant
        # resuming a conversation that actually belongs to a different
        # merchant's customer, require that this customer has previously
        # messaged *this* account before allowing the resume.
        has_history_with_account = (
            db.query(Message)
            .filter(Message.from_phone == phone, Message.account_id == account.id)
            .first()
        )
        if not has_history_with_account:
            return f"No conversation with {phone} found for this account."

        context = (
            db.query(ConversationContext)
            .filter(ConversationContext.customer_id == customer.id)
            .first()
        )
        if not context or context.state != ConversationState.HUMAN_HANDOFF:
            return f"{phone} is not currently in a human handoff state."

        context.state = ConversationState.NEGOTIATING
        context.handoff_at = None
        db.commit()
        return f"Conversation with {phone} resumed. Amara will respond to their next message."

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
            
    # For any other message from merchant, maybe they are replying to a HUMAN_HANDOFF?
    # MVP assumption: If it doesn't match a command, just return help.
    return "Unknown command. Send `/manage` to see available commands."
