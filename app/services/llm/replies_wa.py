import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.attachments import describe_attachments
from app.core.config import settings
from app.core.security import normalize_phone
from app.crud import message as crud_message
from app.db.models import (
    ConversationState,
    Customer,
    Message,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm.helpers import (
    DealRejected,
    _hold_expired,
    _is_out_of_stock,
    _negotiation_floor,
)
from app.services.handoff_flow import consume_directive_wa, format_directive_block
from app.services.llm.markers import (
    _mark_turn_outcome,
    _parse_and_strip_agreement,
    _parse_and_strip_language,
    _parse_and_strip_quote,
    _parse_and_strip_resend,
)
from app.services.llm.prompts import _holding_reply, _outage_reply
from app.services.llm.providers import (
    LLMLengthCutoff,
    LLMUnavailable,
    _complete_with_retry,
    _is_transient_llm_error,
)

logger = logging.getLogger("app.services.llm")


class WAReplyMixin:
    async def generate_response(
        self,
        message_body: str,
        product_slug: Optional[str] = None, 
        knowledge_content: Optional[str] = None,
        system_prompt: Optional[str] = None,
        history: List[dict] = None,
        from_phone: str = None,
        db: Session = None,
        account: WhatsAppAccount = None,
        retry_on_outage: bool = False,
        turn_outcome: Optional[dict] = None,
        proactive: bool = False,
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
            
        # Scoped per (customer, account): a handoff or pending quote at one
        # shop must not leak into this shop's conversation with the same
        # phone number. Legacy NULL-account rows are adopted by the first
        # account that sees them (see crud.message.get_or_create_context).
        context = crud_message.get_or_create_context(db, customer.id, account.id)

        # The customer just sent a new message, so they're no longer
        # "quiet" — proactively clear any outstanding quote-nudge state
        # regardless of what (if anything) this turn's reply ends up being.
        # This is the main defense against a stale nudge firing later; the
        # nudge task itself also re-checks validity at fire time as a
        # second layer (see app/tasks/quote_nudges.py). Skipped for
        # proactive (/direct) turns — no new inbound arrived, so there is
        # no "no longer quiet" signal to act on.
        if context.pending_quote_at is not None and not proactive:
            context.pending_quote_price = None
            context.pending_quote_at = None
            context.quote_nudge_count = 0
            db.commit()

        # If already in handoff, don't generate AI response
        if context.state == ConversationState.HUMAN_HANDOFF:
            # We assume a human agent is dealing with it. 
            return None # Don't send an automated message

        # One-shot operator steering (/direct): consumed here — after the
        # handoff early-return, so a paused conversation keeps its
        # directive until Amara actually generates — and appended to the
        # system prompt below, never to anything customer-facing.
        directive = consume_directive_wa(account.id, customer.id)

        # 1.5 Determine merchant context
        is_platform_account = account.is_platform_account
        
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

        # 2. Fetch products for this merchant (drafts excluded — see above).
        # On the platform account the prospect owns nothing, so the catalog
        # is the Taskflow subscription itself — without this Amara pitches
        # a product she can't see and can never close (#89).
        if is_platform_account:
            from app.services.subscription_access import get_platform_plans

            products = [p for p in get_platform_plans(db) if not p.is_draft]
        else:
            products = (
                db.query(Product)
                .filter(Product.merchant_id == merchant_id, Product.is_draft == False)
                .all()
            )
        if not products:
            catalog_str = "No products currently available."
        else:
            catalog_str = "Available Products:\n"
            for p in products:
                installments_note = "" if p.allow_installments else " (full payment only — installments not offered)"
                stock_note = " (OUT OF STOCK)" if _is_out_of_stock(p) else ""
                stock_display = (
                    "available now, billed monthly" if p.is_subscription else f"Stock: {p.quantity}"
                )
                plan_note = ""
                if p.is_platform_plan:
                    plan_note = (
                        f", includes {p.monthly_reply_limit:,} Amara replies a month"
                        if p.monthly_reply_limit is not None
                        else ", includes unlimited Amara replies"
                    )
                catalog_str += f"- {p.name}: Max Price TZS {p.max_price:,.0f}, Lowest Price You May Offer TZS {_negotiation_floor(p):,.0f}, {stock_display}{plan_note}{stock_note}{installments_note}\n"
                if p.description:
                    catalog_str += f"  {p.description}\n"

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
            text = m.body or describe_attachments(m.attachments)
            # Skip bodies that are empty even after describing attachments
            if not text:
                continue
            sender = "Customer" if m.direction == "inbound" else "Amara"
            chat_history_str += f"{sender}: {text}\n"

        # 4. Build system instruction
        
        if is_platform_account:
            # Every price and reply allowance below comes from the plan
            # catalog above — nothing is hardcoded, so new SKUs (Standard,
            # Pro, …) are pitched correctly the moment an admin adds them.
            choice_note = (
                "\nThere is more than one plan in the catalog — help them pick the right one (match the reply allowance to how busy their shop is) before closing."
                if len(products) > 1
                else ""
            )
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for Taskflow OS.
Your goal is to onboard new merchants and explain the value of having an AI agent handle their WhatsApp sales.

{catalog_str}
{choice_note}
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
                base_instruction += f"""
VOICE — this is you speaking as yourself, not a description of a product:
- First person always. "I" do this — never "Amara does this" or "the platform enables."
- The most persuasive thing you can say is that you bring real money, not just conversation. Every time you make the case for trying you, explicitly say you close sales and get payment moving — not just that you answer messages fast. Use words like "pesa"/"mauzo"/"order halisi" (or "money"/"sales"/"real orders" in English) — don't just describe handling messages.
- Short, WhatsApp-length messages. One idea per message, not paragraphs.
- Lead with the benefit, not the mechanism. "I make sure you never miss a sale" beats "I use AI to process incoming messages."
- Confident, warm, direct. No hedging, no disclaimers about being new or unfinished.
- Reply in the SAME language the customer just used — English message gets an English reply, Swahili gets Swahili. Never switch languages mid-conversation unless they do first. Keep the entire reply in that one language — never mix Swahili and English within a single message.
- If the customer corrects you — what to call them, which language, anything — adapt immediately and never repeat the corrected thing in the same reply.
- Setup help: if the shop has no Instagram or WhatsApp business account yet, offer to help them set one up for free before starting — never promise the product works without one.
- Payment truth: you can't see Tunzaa's side. Never say payment is confirmed, received, or active — the confirmation message arrives separately once approved. After a close, only say the request is sent; if they say they've paid, thank them and say you're confirming it.
- Ask before you tell — a couple of discovery questions earn the right to pitch. Don't front-load everything in one message.
- Never claim a specific number or result you don't actually have for THIS shop (no invented stats or "other shops saw X%"). Speak in outcomes instead ("I catch the messages you're currently losing").
- Never invent a delivery cost, timeframe, or payment option — only quote what the shop has actually told you (via their knowledge base / instructions below). If you don't have it, say you'll confirm rather than making something up.
- When replying in Swahili, phrase things the way a real Tanzanian shop owner actually texts — casual and natural. Don't translate the English example lines below word-for-word; use them for the point being made, not the exact phrasing.
- Before asking anything, check the conversation history above. Never ask a question you've already asked, and never ask about something the customer already told you (even loosely or indirectly) — move on to matching a benefit or continuing naturally instead.
- Don't repeat the same call-to-action (like asking them to try you out) twice in a row. If they respond with a new question instead of answering it, engage fully with that question first — only circle back to the offer later, once, when they actually seem ready. If you do circle back, use different words than last time — never reuse the exact same sentence twice in one conversation.

DISCOVERY — ask about ONE of these before pitching, in your own natural phrasing (only if you don't already have the answer from earlier in the conversation):
- how many customer messages they get in a day
- what eats their time most — replying fast, or the price negotiation itself
- whether they've ever lost track of an interested customer who never followed up

MATCH THE BENEFIT TO WHAT THEY TELL YOU:
- "I get asked the same questions all day" -> "I take care of that — price, availability, how to order — answered the moment they ask, so you're not typing the same reply fifty times a day."
- "Negotiating price takes forever" -> "I can handle the back-and-forth within limits you set, so your customer gets a fast answer and you keep the margin you want."
- "Messages get buried when I'm busy" -> "Nothing gets lost with me. Every customer gets answered, even on your busiest hour of your busiest day."
- "I lose track of what's in stock" -> "I keep a running count as things sell, so you're never guessing what's left."
- "People seem interested but never actually order" -> "I don't stop at answering — I close. I confirm the details and get you a real order, not just a maybe."
- "Sorting out delivery is its own whole conversation" -> "I handle that too — address, timing, cost — directly with the customer, so the order's ready to go instead of stuck in another round of back-and-forth."

HANDLING HESITATION — confident, no hedging:
- "Is this expensive?" -> "Think about it the other way — what's one missed order worth to you? I'm built to pay for myself many times over by catching the sales that are currently slipping past you." If they push for the actual number after this, state it plainly (see Pricing below) — don't dodge a direct question twice.
- "How does this even work?" -> "It's simple. You tell me your prices and how you like to negotiate, I start handling your messages, and you can check in anytime to see exactly what I'm saying to your customers."
- "What if you say something wrong or sell too cheap?" -> "You set my price floor and what I'm allowed to offer. I never go below what you've approved, and you can jump into any conversation and take over whenever you want."
- "I don't trust AI with my customers." -> "Fair — start me on your slowest day of the week and watch what I do before you hand me anything more. You stay in control the whole time."

THE CLOSE, once they seem ready:
"Want to try me on your shop and see what happens? I'll start with your last batch of messages so you can see exactly how I'd have handled them, before anything changes for your customers."

CLOSING MECHANICS — how a yes becomes a subscription:
- Each plan's price is fixed at exactly its catalog Max Price — never negotiate it, never state any other number for that plan.
- This is a subscription, not a physical good: never ask about pickup or delivery, and never offer installments — full payment only.
- Once they explicitly agree to subscribe to a specific plan at its catalog price, confirm it back naturally, then end your response with the exact marker [AGREEMENT_REACHED: product=<exact plan name from the catalog above> price=<that plan's catalog Max Price> payment=full]. Only include this marker once the yes is explicit — never guess or assume it. Emitting it sends them the mobile-money payment request automatically.

OPERATIONAL RULES:
1. Pricing: fixed and simple — state the chosen plan's catalog price plainly and confidently whenever directly asked, do not deflect to the dashboard for it. One flat price per month, *no commission* on their sales. They pay it as a mobile-money payment request to their phone, not taken out of their sales. Their first month starts the day you go live on their shop, not the day they pay, and they get a full refund if they change their mind before then. Never say there's no upfront payment, never say the fee is deducted from sales, and never mention a commission.
2. Onboarding & Setup: You CANNOT add products or register businesses via WhatsApp. If the user asks you to register their business, add a product, or set up their store, you MUST tell them that you cannot do this here and they MUST use the merchant dashboard: {settings.DASHBOARD_URL} . Do NOT pretend or hallucinate that you have added their products or business.
3. How payment actually works — this is real and automatic, state it confidently, never say you have no payment process of your own: once a customer agrees to buy something, a payment request is sent automatically straight to their phone via mobile money for them to approve — no manual bank transfer, cash negotiation, or collection work for the shop owner. Installment plans (Lipa kwa Awamu) are also available if the shop wants to offer them. This is a fixed platform capability, the same for every shop, not something the shop configures.
4. Delivery — this is genuinely NOT automatic, unlike payment: it depends entirely on how the shop already handles delivery. Only state a delivery cost, timeframe, or process the shop has actually told you; otherwise say it follows however the shop currently does it and you'll confirm the details.
5. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
6. Currency: every catalog price above is Tanzanian Shillings, never any other currency. Never say "$", "USD", "dollars", or imply any price is in another currency, and never convert one.
7. Billing cycle: subscriptions are monthly only — there is no yearly plan, no discount for longer terms, and no free trial. If they ask for yearly or any non-monthly term, say plainly it's monthly-only and offer to start them this month. Never invent a yearly price or multiply the monthly price into one.
8. Address: never assume the customer's gender. Use no gendered address term (never kaka/dada) unless they explicitly state their own — default to no address term in English, or ndugu in Swahili. Do not mirror their address terms back: them calling you kaka says nothing about them. If they question or correct how you address them, drop the term instantly, apologize WITHOUT repeating it, and never use it again in this conversation — the correction overrides anything earlier in the chat.
9. Missing payment request: if they say the mobile-money request never arrived on their phone, say you're sorting it out now and end your response with the exact marker [RESEND_PAYMENT: product=<exact plan name from the catalog above> price=<that plan's catalog Max Price> payment=full] repeating the same plan as the original close. Never claim in your own words that you've re-sent it — the confirmation line is added automatically, and only when a resend actually fires.
10. Coming back: if they message again after a goodbye, a no, or a quiet spell — even just "Hello" or a new question — treat it as a fresh start. Answer what they actually asked; never reply with another goodbye or keep pushing the old close.
"""
        else:
            base_instruction = f"""You are Amara, the 24/7 AI Sales Agent for {account.name}.
Your goal is to politely assist customers, answer questions, and sell products from the catalog.

{catalog_str}

VOICE:
- First person always — you work for this shop, speak as yourself, not about "the platform."
- Short, WhatsApp-length messages. One idea per message, not paragraphs.
- Confident and direct. Never invent a delivery cost, timeframe, or payment detail you don't actually have — only use what's in the knowledge/instructions below, or say you'll confirm.

RULES:
1. Selling: Be helpful and persuasive. Try to close the sale. Only describe a product using what's actually in the catalog/knowledge above (name, price, stock, description) — never invent features, specs, or details that aren't there, even ones that sound plausible for a product with that name.
2. Pricing & Negotiation: Negotiate between the catalog's max price and its lowest price you may offer. Never state or agree to a price below that lowest price.
3. Don't just answer and go quiet — push toward a confirmed order. Once the customer has clearly agreed to buy a specific product at a specific price, confirm it back naturally (e.g. "Just to confirm: [item] at [price], sound right?"), ask whether they'd like to pay in full or in installments — unless the catalog marks that product "full payment only", in which case don't offer installments at all; if the customer asks for installments on such a product, tell them this one is full-payment only — then ask if they'd like pickup or delivery. Once you have both a specific agreed price AND a payment method choice, confirm the deal and end your response with the exact marker [AGREEMENT_REACHED: product=<product name> price=<agreed price> payment=<full|installment>] (e.g. [AGREEMENT_REACHED: product=Shoes price=45000 payment=full]). Only include this marker once both are explicit — never guess or assume either.
4. Payment: describe it accurately — a payment request gets sent directly to the customer's phone for them to approve, not a menu of payment apps to choose from. Don't name specific mobile money providers.
5. Delivery: if they want delivery, ask where and roughly when. After the order is confirmed you'll separately ask them to share their exact location via WhatsApp's location-sharing feature — you don't need to collect a written address yourself. Only quote a delivery cost or timeframe if it's actually in your knowledge/instructions below; otherwise say the shop will confirm it.
6. Quoting: whenever you state a specific price to the customer during negotiation but haven't closed the deal yet, end your response with the marker [QUOTE: product=<product name> price=<price>] (e.g. [QUOTE: product=Shoes price=45000]). Don't include this once you've already emitted [AGREEMENT_REACHED] for the same deal.
7. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
8. Currency: all prices are in Tanzanian Shillings (TZS) — always state them exactly as given, including the TZS label. Never say "$", "USD", "dollars", or imply any other currency, and never convert the number into another currency.
9. Out of stock: never offer, quote, or agree a price on a product the catalog marks OUT OF STOCK — if the customer asks about one, first say plainly, naming the product, that it is out of stock, then suggest an in-stock alternative from the catalog instead.
10. Address: never assume the customer's gender. Use no gendered address term (never kaka/dada) unless they explicitly state their own — default to no address term in English, or ndugu in Swahili. Do not mirror their address terms back: them calling you kaka says nothing about them. If they question or correct how you address them, drop the term instantly, apologize WITHOUT repeating it, and never use it again in this conversation — the correction overrides anything earlier in the chat.
11. Setup help: if the shop has no Instagram or WhatsApp business account yet, offer to help them set one up for free before starting — never promise the product works without one.
12. Payment truth: you can't see Tunzaa's side. Never say payment is confirmed, received, or active — the confirmation message arrives separately once approved. After a close, only say the request is sent; if they say they've paid, thank them and say you're confirming it.
13. Missing payment request: if they say the mobile-money request never arrived on their phone, say you're sorting it out now and end your response with the exact marker [RESEND_PAYMENT: product=<product name> price=<agreed price> payment=<full|installment>] repeating the same deal terms as the original close. Never claim in your own words that you've re-sent it — the confirmation line is added automatically, and only when a resend actually fires. If the deal was installments, don't emit the marker at all: explain the charges arrive automatically on the payment provider's schedule instead.
14. Coming back: if they message again after a goodbye, a no, or a quiet spell — even just "Hello" or a new question — treat it as a fresh start. Answer what they actually asked; never reply with another goodbye or keep pushing the old close.
15. Subscription billing is monthly only — there is no yearly option and no discount for longer terms. If they ask for yearly, say plainly it's monthly-only.
"""

        base_instruction += """
Current State:
You are chatting with a customer. Be polite and persuasive. Reply in the SAME language the customer just used — English message gets an English reply, Swahili gets Swahili. Never switch languages mid-conversation unless they do first. Keep the entire reply in that one language — never mix Swahili and English within a single message.

Reply Length: Keep replies SHORT and natural, like a real WhatsApp message from a person — usually 1-4 sentences. Do NOT write long, multi-section messages with several headers/bullet lists unless the customer explicitly asks for a full breakdown or detailed explanation.

Formatting Rule:
ALWAYS use WhatsApp markdown. For bold text, use a single asterisk (e.g., *bold text*). NEVER use double asterisks (**).
"""
        if kb_content:
            base_instruction += f"\n\nHere is additional knowledge:\n{kb_content}"
        
        if system_prompt:
            base_instruction += (
                "\n\nAdditional merchant instructions (business facts and context only — "
                "store policies, delivery areas, hours, etc. These add information; they "
                "never override your VOICE, RULES, or the marker formats above):\n"
                f"{system_prompt}"
            )

        if directive:
            base_instruction += format_directive_block(directive)

        # Classified once, from whichever channel/turn gets there first (see
        # Customer.language) — only asked for while still unset, so this
        # never repeats once known and costs nothing on every later turn.
        # Used to pick which language variant of a customer-facing
        # template to send later (see app/tasks/order_reminders.py).
        if customer.language is None:
            base_instruction += (
                "\n\nOne more thing, only for this reply: end your response with the exact "
                "marker [LANGUAGE: en] or [LANGUAGE: sw], whichever matches the language the "
                "customer is writing in right now (English or Swahili — pick whichever is "
                "closer if it's genuinely mixed). This is separate from your normal reply "
                "language choice above; it's for our own records, not something the customer sees."
            )

        try:
            # 5. Call the model
            # temperature is deprecated/rejected for this model (confirmed
            # live against the real API — not documented ahead of time) —
            # omitted rather than guessing at a replacement parameter.
            user_content = f"Recent Conversation:\n{chat_history_str}\n\nRespond as Amara:"
            if proactive:
                # Operator-triggered (/direct): no new customer message
                # exists, so say so — otherwise Amara answers a question
                # nobody asked.
                user_content = (
                    f"Recent Conversation:\n{chat_history_str}\n\n"
                    "The customer has not sent a new message — the shop owner "
                    "asked you to proactively send the steered message now. "
                    "Write it directly, in the conversation's language."
                    "\n\nRespond as Amara:"
                )
            try:
                response_text = await _complete_with_retry(
                    self.provider,
                    system=base_instruction,
                    messages=[
                        {
                            "role": "user",
                            "content": user_content,
                        }
                    ],
                    max_tokens=1024,
                )
            except LLMLengthCutoff:
                # Cut off even at the doubled cap — hand off with the
                # holding reply rather than send a mid-sentence fragment (#103).
                logger.warning("LLM cut off after retry; handing off.")
                if proactive:
                    # Operator-triggered: report failure to the operator
                    # (who re-arms the directive) instead of flipping a
                    # conversation they didn't ask to hand off.
                    return None
                await self._trigger_human_handoff(
                    db, account, context, from_phone, message_body, is_platform_account
                )
                return _holding_reply(customer.language)
            except Exception as e:
                if retry_on_outage and _is_transient_llm_error(e):
                    raise LLMUnavailable(str(e)) from e
                logger.error(f"LLM error; handing off: {e}")
                if proactive:
                    return None
                await self._trigger_human_handoff(
                    db, account, context, from_phone, message_body, is_platform_account
                )
                return _outage_reply(customer.language)

            if not (response_text or "").strip():
                # Empty even after the inline retry — don't end in
                # silence: hand off and send the holding reply (#101).
                logger.warning("LLM empty after retry; handing off.")
                if proactive:
                    return None
                await self._trigger_human_handoff(
                    db, account, context, from_phone, message_body, is_platform_account
                )
                return _holding_reply(customer.language)

            # Post-process response for special triggers

            # 1.6 Language classification (only requested in the prompt
            # while customer.language is still unset — see below — so this
            # is a no-op on every later turn).
            response_text, detected_language = _parse_and_strip_language(response_text)
            if detected_language and customer.language is None:
                customer.language = detected_language
                db.commit()

            # 2. Human Handoff. Proactive (/direct) turns strip the
            # marker but never trigger — the operator is already here.
            if "[HUMAN_HANDOFF]" in response_text:
                response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
                if not proactive:
                    await self._trigger_human_handoff(
                        db, account, context, from_phone, message_body, is_platform_account
                    )

                if not response_text:
                    response_text = "I am transferring you to a human agent who can help you further. Please hold."
            
            # 3. Deal closing (AGREEMENT_REACHED). Both prompt branches
            # instruct the model to emit this marker — regular merchants
            # for catalog products, the platform account for the Taskflow
            # subscription itself (#89) — but strip it unconditionally
            # regardless of branch as defense in depth against it leaking
            # into a customer-facing message. If a handoff also fired above,
            # handoff wins — don't create an order for a conversation
            # simultaneously being escalated to a human.
            response_text, agreement = _parse_and_strip_agreement(response_text)
            if agreement:
                _mark_turn_outcome(turn_outcome, "markers_seen")
                if proactive:
                    # A proactive turn carries no customer consent — Amara's
                    # words go out, but no order is ever closed from them.
                    logger.warning("AGREEMENT_REACHED in a proactive turn; sending words only, no close.")
                elif context.state == ConversationState.HUMAN_HANDOFF:
                    logger.warning("AGREEMENT_REACHED marker seen on a handed-off turn; ignoring.")
                else:
                    try:
                        note = await self._close_deal(
                            db, account, customer, normalized_from_phone, agreement, context
                        )
                    except Exception as deal_err:
                        # Amara already told the buyer it's a deal — don't
                        # let that stand with nothing following (#37).
                        (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                            f"Deal not closed; handing off: {deal_err}"
                        )
                        response_text = _holding_reply(customer.language)
                        await self._trigger_human_handoff(
                            db, account, context, from_phone, message_body, is_platform_account
                        )
                        note = None
                    if note:
                        _mark_turn_outcome(turn_outcome, "order_action")
                        response_text = f"{response_text}\n\n{note}".strip()

            # 3b. Payment resend (RESEND_PAYMENT): the buyer says the
            # request never arrived. Skipped when this turn already
            # closed (a fresh prompt just went out) or handed off.
            # Three outcomes, in order: re-fire against this chat's live
            # order; dispatch onto it if the first attempt never went
            # out; or — when the close never landed at all (the pre-#113
            # silent-marker failure) — close fresh from the marker's deal
            # terms, so Amara is never left telling the buyer "it's on
            # its way" with nothing behind it.
            response_text, resend = _parse_and_strip_resend(response_text)
            if resend is not None and proactive:
                logger.warning("RESEND_PAYMENT in a proactive turn; sending words only, no re-fire.")
            elif resend is not None and not agreement and context.state != ConversationState.HUMAN_HANDOFF:
                _mark_turn_outcome(turn_outcome, "markers_seen")
                live_order = (
                    db.query(Order)
                    .filter(
                        Order.account_id == account.id,
                        Order.customer_id == customer.id,
                        Order.status == OrderStatus.PENDING,
                        Order.payment_type == PaymentType.FULL,
                    )
                    .order_by(Order.created_at.desc())
                    .first()
                )
                if live_order is not None and _hold_expired(live_order):
                    # A weeks-old hold, not this deal — the fallback below
                    # closes fresh (and the reclose guard retires this row
                    # when it does).
                    live_order = None
                if live_order is not None and live_order.product is None:
                    logger.warning(
                        "Ignoring live RESEND_PAYMENT order %s: product row is gone.", live_order.id
                    )
                    live_order = None
                if live_order is not None and resend.get("product_name"):
                    # The marker names a different deal than the live order
                    # (abandoned cart for A, now closing B) — re-firing the
                    # wrong prompt would charge for the wrong thing, so fall
                    # through to a fresh close for the named deal instead.
                    if (
                        live_order.product.name.lower() != resend["product_name"].lower()
                        or abs((live_order.agreed_price or 0) - resend["price"]) > 0.01
                    ):
                        logger.warning(
                            "RESEND_PAYMENT names %r at %s but live order %s is %r at %s; closing fresh.",
                            resend["product_name"],
                            resend["price"],
                            live_order.id,
                            live_order.product.name,
                            live_order.agreed_price,
                        )
                        live_order = None
                resend_note = None
                if live_order is not None and live_order.tunzaa_transaction_id:
                    resend_note = await self._resend_payment_for_order(
                        db, live_order, normalized_from_phone, customer.language
                    )
                elif live_order is not None:
                    resend_note = await self._dispatch_payment(
                        db,
                        live_order,
                        live_order.product,
                        normalized_from_phone,
                        PaymentType.FULL,
                        customer.language,
                    )
                elif resend.get("product_name"):
                    if resend.get("payment_type") == "installment":
                        # Plan legs are pushed on Tunzaa's own schedule —
                        # re-firing would rebill a leg. The model's own
                        # words go out alone; the prompt tells it to explain
                        # the schedule rather than promise a resend.
                        logger.warning("Ignoring RESEND_PAYMENT for an installment deal; legs are Tunzaa-pushed.")
                    else:
                        try:
                            resend_note = await self._close_deal(
                                db,
                                account,
                                customer,
                                normalized_from_phone,
                                {
                                    "product_name": resend["product_name"],
                                    "price": resend["price"],
                                    "payment_type": "full",
                                    "phone": None,
                                },
                                context,
                            )
                        except Exception as deal_err:
                            # Same contract as a failed agreement close: the
                            # buyer is stuck mid-payment, so a human takes it.
                            (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                                f"Resend fallback close failed; handing off: {deal_err}"
                            )
                            response_text = _holding_reply(customer.language)
                            await self._trigger_human_handoff(
                                db, account, context, from_phone, message_body, is_platform_account
                            )
                else:
                    logger.warning(
                        "Ignoring RESEND_PAYMENT: no live PENDING order for this chat and no deal terms to close fresh."
                    )
                if resend_note:
                    _mark_turn_outcome(turn_outcome, "order_action")
                    response_text = f"{response_text}\n\n{resend_note}".strip()

            # 4. Price quoted but not yet agreed (QUOTE). Superseded by a
            # handoff or an agreement reached in this same turn — either
            # means there's nothing to nudge about.
            response_text, quote = _parse_and_strip_quote(response_text)
            if quote and not agreement and not is_platform_account and context.state != ConversationState.HUMAN_HANDOFF and not proactive:
                await self._track_quote(
                    db, account, context, quote, from_phone, message_body, is_platform_account
                )

            return response_text
        except LLMUnavailable:
            raise
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."
