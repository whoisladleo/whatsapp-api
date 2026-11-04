import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.attachments import describe_attachments
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
from app.services.llm.conversation import (
    CHANNEL_WHATSAPP,
    build_merchant_prompt,
    build_platform_prompt,
    render_catalog,
)
from app.services.llm.helpers import DealRejected
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
        catalog_str = render_catalog(products)

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
        
        # The prompt text lives in conversation.py now (one builder per
        # account type, parameterized by channel). The one-shot language
        # classification is only requested while still unset — it never
        # repeats once known and costs nothing on every later turn. Used to
        # pick which language variant of a customer-facing template to send
        # later (see app/tasks/order_reminders.py).
        if is_platform_account:
            base_instruction = build_platform_prompt(
                channel=CHANNEL_WHATSAPP,
                catalog_str=catalog_str,
                is_first_message=is_first_message,
                multi_plan=len(products) > 1,
                knowledge_content=kb_content,
                directive_block=format_directive_block(directive) if directive else None,
                merchant_instructions=system_prompt,
                need_language_marker=customer.language is None,
            )
        else:
            base_instruction = build_merchant_prompt(
                channel=CHANNEL_WHATSAPP,
                account_name=account.name,
                catalog_str=catalog_str,
                knowledge_content=kb_content,
                directive_block=format_directive_block(directive) if directive else None,
                merchant_instructions=system_prompt,
                need_language_marker=customer.language is None,
            )

        # Tail, knowledge, instructions, directive, and the one-shot language
        # marker all ship inside the built prompt (see the builders above).

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
                async def _close_fresh(marker_terms: dict) -> Optional[str]:
                    return await self._close_deal(
                        db,
                        account,
                        customer,
                        normalized_from_phone,
                        {
                            "product_name": marker_terms["product_name"],
                            "price": marker_terms["price"],
                            "payment_type": "full",
                            "phone": None,
                        },
                        context,
                    )

                try:
                    resend_note = await self._resend_payment_note(
                        db,
                        live_order=live_order,
                        resend=resend,
                        phone_for_dispatch=normalized_from_phone,
                        fallback_phone=normalized_from_phone,
                        language=customer.language,
                        close_fresh_fn=_close_fresh,
                    )
                except Exception as deal_err:
                    # Same contract as a failed agreement close: the buyer is
                    # stuck mid-payment, so a human takes it.
                    (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                        f"Resend fallback close failed; handing off: {deal_err}"
                    )
                    response_text = _holding_reply(customer.language)
                    await self._trigger_human_handoff(
                        db, account, context, from_phone, message_body, is_platform_account
                    )
                    resend_note = None
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
