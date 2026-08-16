import anthropic
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from app.core.config import settings
from app.core.security import normalize_phone
from sqlalchemy.orm import Session
from app.db.models import (
    Customer,
    ConversationContext,
    ConversationState,
    Installment,
    InstallmentStatus,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    WhatsAppAccount,
    Message,
    User,
)

logger = logging.getLogger(__name__)

# How long a reservation (and the matching order_reminders schedule) holds
# stock before auto_cancel_order releases it — see app/tasks/order_reminders.py.
RESERVATION_WINDOW = timedelta(hours=48)

_AGREEMENT_RE = re.compile(
    r"\[AGREEMENT_REACHED:\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.]+)\s+payment=(?P<payment>full|installment)\s*\]",
    re.IGNORECASE,
)


def _parse_and_strip_agreement(text: str):
    """
    Extracts the [AGREEMENT_REACHED: product=... price=... payment=...]
    marker the model is instructed to emit once a deal closes (regular-
    merchant branch only — see the system prompt below), the same way
    [HUMAN_HANDOFF] is parsed. Always strips the marker from what the
    customer sees, whether or not it parses; a malformed marker just means
    no order gets created.
    """
    match = _AGREEMENT_RE.search(text)
    if not match:
        return text, None

    cleaned = _AGREEMENT_RE.sub("", text).strip()

    try:
        price = float(match.group("price"))
    except ValueError:
        return cleaned, None

    product_name = match.group("product").strip()
    if not product_name:
        return cleaned, None

    return cleaned, {
        "product_name": product_name,
        "price": price,
        "payment_type": match.group("payment").lower(),
    }

class LLMService:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.model_id = 'claude-sonnet-5'

    def _get_knowledge_base(self, product_slug: str) -> str:
        kb_path = f"knowledge/{product_slug}.md"
        if not os.path.exists(kb_path):
            return ""
        
        with open(kb_path, 'r') as f:
            return f.read()

    async def _close_deal(
        self,
        db: Session,
        account: WhatsAppAccount,
        customer: Customer,
        normalized_from_phone: str,
        agreement: dict,
        context: ConversationContext,
    ) -> Optional[str]:
        """
        Validates a parsed AGREEMENT_REACHED marker against the real catalog
        (server-side enforcement of min/max price and stock — the prompt's
        "SECRET FLOOR" instruction alone doesn't stop a prompt-injected
        model from claiming an out-of-band price), reserves stock, creates
        the Order, and dispatches the real Tunzaa payment. Returns a short
        note to append to the customer-facing reply, or None if nothing
        happened (invalid agreement — the model's own reply still goes out
        as-is; this is expected to be rare/adversarial-only since the model
        is instructed to stay within the catalog's own bounds).
        """
        product = (
            db.query(Product)
            .filter(Product.merchant_id == account.user_id, Product.name.ilike(agreement["product_name"]))
            .first()
        )
        price = agreement["price"]
        if not product:
            logger.warning(f"AGREEMENT_REACHED for unknown product '{agreement['product_name']}'; ignoring.")
            return None
        if not (product.min_price <= price <= product.max_price):
            logger.warning(
                f"AGREEMENT_REACHED price {price} outside [{product.min_price}, {product.max_price}] "
                f"for '{product.name}'; ignoring (possible prompt injection)."
            )
            return None
        if product.quantity < 1:
            logger.warning(f"AGREEMENT_REACHED for '{product.name}' but it's out of stock; ignoring.")
            return None

        payment_type = (
            PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
        )

        # Reserve: decrement stock now (soft-lock during the hold window,
        # not deferred to payment success) so a second customer can't be
        # sold the same unit while this one is paying.
        product.quantity -= 1
        order = Order(
            customer_id=customer.id,
            product_id=product.id,
            account_id=account.id,
            quantity=1,
            agreed_price=price,
            payment_type=payment_type,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + RESERVATION_WINDOW,
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        context.last_offer = price
        db.commit()

        self._schedule_reminders(order.id)

        note = await self._dispatch_payment(db, order, product, normalized_from_phone, payment_type)
        return note

    async def _dispatch_payment(
        self, db: Session, order: Order, product: Product, phone: str, payment_type: PaymentType
    ) -> str:
        from app.services.tunzaa import tunzaa_service

        try:
            if payment_type == PaymentType.FULL:
                resp = await tunzaa_service.initiate_payment(
                    phone=phone, amount=order.agreed_price, reference=f"order-{order.id}"
                )
                order.tunzaa_transaction_id = resp.get("transactionID")
                db.commit()
                return (
                    f"A payment request for TZS {order.agreed_price:,.0f} has been sent to your "
                    "phone — please approve it to complete your order. While you're at it, please "
                    "share your delivery location using WhatsApp's location-sharing feature (the 📎 "
                    "attach icon) so we know where to deliver it."
                )
            else:
                resp = await tunzaa_service.create_installment_plan(order, product.name)
                order.tunzaa_plan_id = str(resp.get("plan", {}).get("plan_id")) if resp.get("plan", {}).get("plan_id") else None
                for inst in resp.get("installments", []):
                    try:
                        due_date = datetime.strptime(inst["due_date"], "%Y-%m-%d")
                    except (KeyError, ValueError):
                        due_date = datetime.utcnow()
                    db.add(
                        Installment(
                            order_id=order.id,
                            tunzaa_installment_id=str(inst.get("installment_id")) if inst.get("installment_id") else None,
                            amount_due=float(inst.get("amount", 0) or 0),
                            due_date=due_date,
                            status=InstallmentStatus.PENDING,
                        )
                    )
                db.commit()
                return (
                    "Your installment plan has been created! We'll be in touch with payment "
                    "details. Please also share your delivery location using WhatsApp's "
                    "location-sharing feature (the 📎 attach icon) so we know where to deliver it."
                )
        except Exception as e:
            logger.error(f"Tunzaa dispatch failed for order {order.id}: {e}")
            # This order never got as far as an accepted payment attempt —
            # release the reservation rather than leave it dangling.
            product.quantity += order.quantity or 1
            order.status = OrderStatus.CANCELLED
            db.commit()
            return "Something went wrong setting up your payment — please try again in a moment."

    def _schedule_reminders(self, order_id: int) -> None:
        from app.tasks.order_reminders import (
            SCARCITY_NUDGE_DELAY_SECONDS,
            SOFT_NUDGE_DELAY_SECONDS,
            AUTO_CANCEL_DELAY_SECONDS,
            auto_cancel_order,
            send_scarcity_nudge,
            send_soft_nudge,
        )
        for task, delay in (
            (send_soft_nudge, SOFT_NUDGE_DELAY_SECONDS),
            (send_scarcity_nudge, SCARCITY_NUDGE_DELAY_SECONDS),
            (auto_cancel_order, AUTO_CANCEL_DELAY_SECONDS),
        ):
            try:
                task.apply_async(args=[order_id], countdown=delay)
            except Exception as sched_err:
                logger.error(f"Failed to schedule {task.name}: {sched_err}")

    async def generate_response(
        self, 
        message_body: str, 
        product_slug: Optional[str] = None, 
        knowledge_content: Optional[str] = None,
        system_prompt: Optional[str] = None,
        history: List[dict] = None,
        from_phone: str = None,
        db: Session = None,
        account: WhatsAppAccount = None
    ) -> str:
        if not self.client:
            return "AI service is currently unavailable."
            
        # 1. Fetch customer & context
        # Comparisons/creation use the normalized form so numbers that reach
        # us in different formats (e.g. leading-0 vs 255 country code) still
        # resolve to the same Customer going forward — see
        # app.core.security.normalize_phone for why storage itself isn't
        # touched, only lookups/writes made from here on.
        normalized_from_phone = normalize_phone(from_phone)
        customer = db.query(Customer).filter(Customer.phone_number == normalized_from_phone).first()
        if not customer:
            customer = Customer(phone_number=normalized_from_phone)
            db.add(customer)
            db.commit()
            db.refresh(customer)
            
        context = db.query(ConversationContext).filter(ConversationContext.customer_id == customer.id).first()
        if not context:
            context = ConversationContext(customer_id=customer.id)
            db.add(context)
            db.commit()
            db.refresh(context)

        # If already in handoff, don't generate AI response
        if context.state == ConversationState.HUMAN_HANDOFF:
            # We assume a human agent is dealing with it. 
            return None # Don't send an automated message

        # 1.5 Determine merchant context
        is_platform_account = (account.name.lower() == 'taskflow')
        
        merchant_user = None
        if is_platform_account:
            # The person texting IS the merchant. Note: this normalizes for
            # lookup/creation here, but app/api/v1/auth.py's OTP login does
            # not normalize `username` — a merchant created via this path
            # with one phone format could theoretically fail to match a
            # differently-formatted login attempt. Fixing that fully would
            # mean normalizing auth.py's username handling too, which is
            # out of scope for this change.
            merchant_user = db.query(User).filter(User.username == normalized_from_phone).first()
            if not merchant_user:
                merchant_user = User(
                    username=normalized_from_phone
                )
                db.add(merchant_user)
                db.commit()
                db.refresh(merchant_user)
            merchant_id = merchant_user.id
        else:
            # The person texting is a customer, the merchant is the account owner
            merchant_id = account.user_id

        # 2. Fetch products for this merchant
        products = db.query(Product).filter(Product.merchant_id == merchant_id).all()
        if not products:
            catalog_str = "No products currently available."
        else:
            catalog_str = "Available Products:\n"
            for p in products:
                catalog_str += f"- {p.name}: Max Price {p.max_price}, Min Price (SECRET FLOOR) {p.min_price}, Stock: {p.quantity}\n"

        # 3. Prepare knowledge base
        kb_content = knowledge_content or ""
        if not kb_content and product_slug:
            kb_content = self._get_knowledge_base(product_slug)
        
        # Check if we have ever replied to this user
        outbound_count = db.query(Message).filter(
            Message.to_phone == from_phone,
            Message.account_id == account.id,
            Message.direction == "outbound"
        ).count()
        is_first_message = (outbound_count == 0)

        # Build chat history string for context
        history_msgs = db.query(Message).filter(
            ((Message.from_phone == from_phone) | (Message.to_phone == from_phone)),
            Message.account_id == account.id
        ).order_by(Message.timestamp.asc()).limit(20).all()
        
        chat_history_str = ""
        for m in history_msgs:
            # Skip empty bodies
            if not m.body:
                continue
            sender = "Customer" if m.direction == "inbound" else "Amara"
            chat_history_str += f"{sender}: {m.body}\n"

        # 4. Build system instruction
        
        if is_platform_account:
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for Taskflow OS.
Your goal is to onboard new merchants and explain the value of having an AI agent handle their WhatsApp sales.

{catalog_str}
"""
            
            if is_first_message:
                base_instruction += """
RULES:
1. Welcome Message: This is the very first time we are replying. You MUST reply with EXACTLY the text below, word-for-word, based on the user's language (default to Swahili). DO NOT add any other text or greetings.

[English Welcome]
Hi, I'm *Amara*. I help shops answer customers instantly, handle the back-and-forth on price, and make sure no message gets lost — even on your busiest days. Mind if I ask a couple of quick questions first?

[Swahili Welcome]
Habari! Naitwa *Amara*. Ninasaidia maduka kujibu wateja papo hapo, kushughulikia mazungumzo ya bei, na kuhakikisha hakuna ujumbe unaopotea — hata siku zenye shughuli nyingi. Naomba nikuulize maswali machache kwanza?
"""
            else:
                base_instruction += """
VOICE — this is you speaking as yourself, not a description of a product:
- First person always. "I" do this — never "Amara does this" or "the platform enables."
- Short, WhatsApp-length messages. One idea per message, not paragraphs.
- Lead with the benefit, not the mechanism. "I make sure you never miss a sale" beats "I use AI to process incoming messages."
- Confident, warm, direct. No hedging, no disclaimers about being new or unfinished.
- Ask before you tell — a couple of discovery questions earn the right to pitch. Don't front-load everything in one message.
- Never claim a specific number or result you don't actually have for THIS shop (no invented stats or "other shops saw X%"). Speak in outcomes instead ("I catch the messages you're currently losing").

DISCOVERY — if you don't already know, ask one of these before pitching:
- About how many messages do they get from customers in a day?
- What eats the most time — replying fast, or the price negotiation itself?
- Have they ever lost track of an interested customer who never followed up?

MATCH THE BENEFIT TO WHAT THEY TELL YOU:
- "I get asked the same questions all day" -> "I take care of that — price, availability, how to order — answered the moment they ask, so you're not typing the same reply fifty times a day."
- "Negotiating price takes forever" -> "I can handle the back-and-forth within limits you set, so your customer gets a fast answer and you keep the margin you want."
- "Messages get buried when I'm busy" -> "Nothing gets lost with me. Every customer gets answered, even on your busiest hour of your busiest day."
- "I lose track of what's in stock" -> "I keep a running count as things sell, so you're never guessing what's left."

HANDLING HESITATION — confident, no hedging:
- "Is this expensive?" -> "Think about it the other way — what's one missed order worth to you? I'm built to pay for myself many times over by catching the sales that are currently slipping past you." If they push for the actual number after this, state it plainly (see Pricing below) — don't dodge a direct question twice.
- "How does this even work?" -> "It's simple. You tell me your prices and how you like to negotiate, I start handling your messages, and you can check in anytime to see exactly what I'm saying to your customers."
- "What if you say something wrong or sell too cheap?" -> "You set my price floor and what I'm allowed to offer. I never go below what you've approved, and you can jump into any conversation and take over whenever you want."
- "I don't trust AI with my customers." -> "Fair — start me on your slowest day of the week and watch what I do before you hand me anything more. You stay in control the whole time."

THE CLOSE, once they seem ready:
"Want to try me on your shop and see what happens? I'll start with your last batch of messages so you can see exactly how I'd have handled them, before anything changes for your customers."

OPERATIONAL RULES:
1. Pricing: fixed and simple — state it plainly and confidently whenever directly asked, do not deflect to the dashboard for it: *Zero Upfront Fee* (0 TZS to start) and a *Monthly Fee of 100,000 TZS* (auto-deducted directly from sales, no separate invoice).
2. Onboarding & Setup: You CANNOT add products or register businesses via WhatsApp. If the user asks you to register their business, add a product, or set up their store, you MUST tell them that you cannot do this here and they MUST use the merchant dashboard: https://dashboard-taskflow.whoisladleo.dev/ . Do NOT pretend or hallucinate that you have added their products or business.
3. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
"""
        else:
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for {account.name}.
Your goal is to politely assist customers, answer questions, and sell products from the catalog.

{catalog_str}

RULES:
1. Selling: Be helpful and persuasive. Try to close the sale.
2. Pricing & Negotiation: Use the provided catalog max and min prices to negotiate. Never sell below the minimum price.
3. Closing the Deal: Once the customer has clearly agreed to buy a specific product at a specific price, ask whether they'd like to pay in full or in installments. Once they answer, confirm the deal and end your response with the exact marker [AGREEMENT_REACHED: product=<product name> price=<agreed price> payment=<full|installment>] (e.g. [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]). Only include this marker once you have both a specific agreed price AND a payment method choice from the customer — never guess or assume either.
4. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
"""
        
        base_instruction += """
Current State:
You are chatting with a customer. Be polite, persuasive, and use Swahili or English depending on their language.

Reply Length: Keep replies SHORT and natural, like a real WhatsApp message from a person — usually 1-4 sentences. Do NOT write long, multi-section messages with several headers/bullet lists unless the customer explicitly asks for a full breakdown or detailed explanation.

Formatting Rule:
ALWAYS use WhatsApp markdown. For bold text, use a single asterisk (e.g., *bold text*). NEVER use double asterisks (**).
"""
        if kb_content:
            base_instruction += f"\n\nHere is additional knowledge:\n{kb_content}"
        
        if system_prompt:
            base_instruction += f"\n\nAdditional merchant instructions: {system_prompt}"
        
        try:
            # 5. Call Claude
            # temperature is deprecated/rejected for this model (confirmed
            # live against the real API — not documented ahead of time) —
            # omitted rather than guessing at a replacement parameter.
            response = await self.client.messages.create(
                model=self.model_id,
                max_tokens=1024,
                system=base_instruction,
                messages=[
                    {
                        "role": "user",
                        "content": f"Recent Conversation:\n{chat_history_str}\n\nRespond as Amara:",
                    }
                ],
            )

            # This model returns extended-thinking content blocks before the
            # text block non-deterministically (confirmed live — the same
            # prompt sometimes returns [ThinkingBlock, TextBlock] and
            # sometimes just [TextBlock]), so content[0] isn't reliably the
            # reply — filter for the actual text block(s) instead.
            response_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            
            # Post-process response for special triggers
            
            # 2. Human Handoff
            if "[HUMAN_HANDOFF]" in response_text:
                context.state = ConversationState.HUMAN_HANDOFF
                context.handoff_at = datetime.utcnow()
                db.commit()
                response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()

                # Alert the merchant
                if not is_platform_account and account.owner:
                    merchant_phone = account.owner.username
                    alert_message = f"🚨 *HUMAN HANDOFF REQUIRED* 🚨\nA customer at {from_phone} needs your help!\n\n*Last AI message:* {response_text}"
                    from app.core.provider import WhatsAppProvider
                    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
                    try:
                        alert_resp = await provider.send_text(merchant_phone, alert_message)
                    except Exception as alert_err:
                        print(f"Failed to send handoff alert to merchant: {alert_err}")
                        alert_resp = None

                    from app.crud import message as crud_message
                    crud_message.create_message(
                        db,
                        {
                            "to_phone": merchant_phone,
                            "type": "text",
                            "body": alert_message,
                            "direction": "outbound",
                            "status": "sent" if alert_resp else "failed",
                            "raw": alert_resp or {},
                        },
                        account.id,
                    )

                    from app.tasks.handoff_reminders import (
                        send_handoff_reminder,
                        HANDOFF_REMINDER_DELAY_SECONDS,
                    )
                    try:
                        send_handoff_reminder.apply_async(
                            args=[context.id], countdown=HANDOFF_REMINDER_DELAY_SECONDS
                        )
                    except Exception as sched_err:
                        print(f"Failed to schedule handoff reminder: {sched_err}")

                if not response_text:
                    response_text = "I am transferring you to a human agent who can help you further. Please hold."
            
            # 3. Deal closing (AGREEMENT_REACHED). Only the regular-merchant
            # prompt branch above ever instructs the model to emit this
            # marker (the platform account sells the Taskflow subscription,
            # not the catalog products), but strip it unconditionally
            # regardless of branch as defense in depth against it leaking
            # into a customer-facing message. If a handoff also fired above,
            # handoff wins — don't create an order for a conversation
            # simultaneously being escalated to a human.
            response_text, agreement = _parse_and_strip_agreement(response_text)
            if agreement:
                if is_platform_account or context.state == ConversationState.HUMAN_HANDOFF:
                    logger.warning(
                        "AGREEMENT_REACHED marker seen on an ineligible turn "
                        f"(is_platform_account={is_platform_account}, "
                        f"handoff={context.state == ConversationState.HUMAN_HANDOFF}); ignoring."
                    )
                else:
                    try:
                        note = await self._close_deal(
                            db, account, customer, normalized_from_phone, agreement, context
                        )
                    except Exception as deal_err:
                        logger.error(f"Order/payment completion failed: {deal_err}")
                        note = None
                    if note:
                        response_text = f"{response_text}\n\n{note}".strip()

            return response_text
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."

llm_service = LLMService(api_key=settings.ANTHROPIC_API_KEY) if settings.ANTHROPIC_API_KEY else None
