import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.models import (
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
)
from app.services.handoff_flow import consume_directive_ig, format_directive_block
from app.services.llm.helpers import (
    DealRejected,
    _hold_expired,
    _is_out_of_stock,
    _negotiation_floor,
)
from app.services.llm.markers import (
    _mark_turn_outcome,
    _parse_and_strip_agreement,
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


class DMReplyMixin:
    async def generate_dm_response(
        self,
        message_body: str,
        db: Session,
        account: InstagramAccount,
        customer: InstagramCustomer,
        context: InstagramConversationContext,
        conversation_history: List[dict],
        account_name: str = "the shop",
        knowledge_content: Optional[str] = None,
        retry_on_outage: bool = False,
        turn_outcome: Optional[dict] = None,
        proactive: bool = False,
    ) -> Optional[str]:
        """
        Generates Amara's reply to an ongoing Instagram DM conversation:
        Q&A, price negotiation, and — once the customer is ready to buy —
        closing the deal for real (see _close_instagram_deal), the same way
        generate_response's WhatsApp flow does. The one real difference is
        that Instagram gives no phone number up front, so the prompt below
        has the model ask for one before it's allowed to emit
        [AGREEMENT_REACHED]. Falls back to [HUMAN_HANDOFF] for anything the
        model shouldn't handle itself (unreasonable customers, questions
        outside the catalog/knowledge, or an explicit request for a human).
        Returns None if no client is configured (caller sends nothing). A
        transient Claude failure raises LLMUnavailable when retry_on_outage
        is set; otherwise any Claude failure hands off and returns the
        outage reply.
        """
        if not self.client:
            return None

        # One-shot operator steering (/direct), consumed on this generated
        # reply — the handoff/quota callers return before reaching here,
        # so a paused conversation keeps its directive until Amara
        # actually generates. Appended to the system prompt below, never
        # to anything customer-facing.
        directive = consume_directive_ig(customer.id)

        # Drafts excluded — see the comment-reply builder above.
        products = (
            db.query(Product)
            .filter(Product.merchant_id == account.user_id, Product.is_draft == False)
            .all()
        )
        if not products:
            catalog_str = "No products currently available."
        else:
            catalog_str = "Available Products:\n"
            for p in products:
                installments_note = "" if p.allow_installments else " (full payment only — installments not offered)"
                stock_note = " (OUT OF STOCK)" if _is_out_of_stock(p) else ""
                # Subscriptions have no units — a raw "Stock: 0" invites
                # out-of-stock claims on plans (#92). Mirrors the WhatsApp
                # catalog builder's wording.
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

        history_str = ""
        for m in conversation_history:
            sender = "Customer" if m["direction"] == "inbound" else "Amara"
            history_str += f"{sender}: {m['body']}\n"

        instruction = f"""You are Amara, the AI sales agent for {account_name}, replying to a customer over Instagram Direct Message.

{catalog_str}
"""
        if knowledge_content:
            instruction += f"\nHere is additional knowledge:\n{knowledge_content}"

        if directive:
            instruction += format_directive_block(directive)

        instruction += """
RULES:
1. Be helpful and friendly. Answer questions about products, pricing, and availability using only the catalog/knowledge above — never invent a product, price, stock level, or feature/detail that isn't there.
2. Negotiation: negotiate between the catalog's max price and its lowest price you may offer. Never state or agree to a price below that lowest price.
3. Closing: once the customer has clearly agreed on a specific product and price, ask whether they'd like to pay in full or in installments — unless the catalog marks that product "full payment only", in which case don't offer installments at all; if the customer asks for installments on such a product, tell them this one is full-payment only. Instagram doesn't give us their phone number automatically, so if you don't already have it from earlier in this conversation, ask for it too — explain it's needed to send the mobile-money payment request. Once you have a specific agreed price, a payment method choice, AND a phone number, confirm the deal and end your response with the exact marker [AGREEMENT_REACHED: product=<product name> price=<agreed price> payment=<full|installment> phone=<phone number>] (e.g. [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255700000000]). Only include this marker once all four are explicit — never guess or assume any of them.
4. Payment: describe it accurately — a payment request gets sent directly to the given phone number for them to approve, not a menu of payment apps to choose from. Don't name specific mobile money providers.
5. Human Handoff: if the customer is unreasonable, has complex questions you can't answer from the catalog/knowledge above, or explicitly asks for a human, include the exact marker [HUMAN_HANDOFF]. Don't include this for a normal close — that's handled by AGREEMENT_REACHED instead.
6. Keep replies SHORT and natural, like a real Instagram DM — usually 1-3 sentences. Reply in the SAME language the customer just used — English message gets an English reply, Swahili gets Swahili. Never default to Swahili for an English message. Keep the entire reply in that one language — never mix Swahili and English within a single message.
7. Currency: all prices are in Tanzanian Shillings (TZS) — always state them exactly as given, including the TZS label. Never say "$", "USD", "dollars", or imply any other currency, and never convert the number into another currency.
8. Quoting: whenever you state a specific price for a product during negotiation but haven't closed the deal yet, end your response with the marker [QUOTE: product=<product name> price=<price>] (e.g. [QUOTE: product=Shoes price=45000]). Don't include this once you've emitted [AGREEMENT_REACHED] for the same deal.
9. Out of stock: never offer, quote, or agree a price on a product the catalog marks OUT OF STOCK — if the customer asks about one, first say plainly, naming the product, that it is out of stock, then suggest an in-stock alternative from the catalog instead.
10. Address: never assume the customer's gender. Use no gendered address term (never kaka/dada) unless they explicitly state their own — default to no address term in English, or ndugu in Swahili. Do not mirror their address terms back: them calling you kaka says nothing about them. If they question or correct how you address them, drop the term instantly, apologize WITHOUT repeating it, and never use it again in this conversation — the correction overrides anything earlier in the chat.
11. Setup help: if the shop has no Instagram or WhatsApp business account yet, offer to help them set one up for free before starting — never promise the product works without one.
12. Payment truth: you can't see Tunzaa's side. Never say payment is confirmed, received, or active — the confirmation message arrives separately once approved. After a close, only say the request is sent; if they say they've paid, thank them and say you're confirming it.
13. Missing payment request: if they say the mobile-money request never arrived on their phone, say you're sorting it out now and end your response with the exact marker [RESEND_PAYMENT: product=<product name> price=<agreed price> payment=<full|installment> phone=<phone number>] repeating the same deal terms and phone number as the original close. The phone number is required — without it nothing can be re-sent. Never claim in your own words that you've re-sent it — the confirmation line is added automatically, and only when a resend actually fires. If the deal was installments, don't emit the marker at all: explain the charges arrive automatically on the payment provider's schedule instead.
14. Coming back: if they message again after a goodbye, a no, or a quiet spell — even just "Hello" or a new question — treat it as a fresh start. Answer what they actually asked; never reply with another goodbye or keep pushing the old close.
15. Subscription billing is monthly only — there is no yearly option and no discount for longer terms. If they ask for yearly, say plainly it's monthly-only.

Reply with ONLY the message text (plus whichever marker applies, if any) — no other formatting.
"""

        user_content = f'Recent conversation:\n{history_str}\nCustomer just sent: "{message_body}"\n\nRespond as Amara:'
        if proactive:
            # Operator-triggered (/direct): no new DM exists, so the
            # "just sent" framing would be a lie — say so instead.
            user_content = (
                f"Recent conversation:\n{history_str}\n"
                "The customer has not sent a new message — the shop owner "
                "asked you to proactively send the steered message now. "
                "Write it directly, in the conversation's language."
                "\n\nRespond as Amara:"
            )
        try:
            response_text = await _complete_with_retry(
                self.provider,
                system=instruction,
                messages=[
                    {
                        "role": "user",
                        "content": user_content,
                    }
                ],
                max_tokens=500,
            )
        except LLMLengthCutoff:
            # Cut off even at the doubled cap — hand off with the holding
            # reply rather than send a mid-sentence fragment (#103).
            logger.warning("DM-response LLM cut off after retry; handing off.")
            if proactive:
                # Operator-triggered: report failure to the operator
                # (who re-arms the directive) instead of flipping a
                # conversation they didn't ask to hand off.
                return None
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _holding_reply(None)
        except Exception as e:
            if retry_on_outage and _is_transient_llm_error(e):
                raise LLMUnavailable(str(e)) from e
            logger.error(f"DM-response LLM error; handing off: {e}")
            if proactive:
                return None
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _outage_reply(None)

        if not (response_text or "").strip():
            # Empty even after the inline retry — don't end in silence:
            # hand off (the holding line promises a human follows up) and
            # send the same holding reply a failed close uses (#101).
            logger.warning("DM-response LLM empty after retry; handing off.")
            if proactive:
                return None
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _holding_reply(None)

        # Stripped up front so the markers can never leak through the
        # early handoff return below.
        response_text, quote = _parse_and_strip_quote(response_text)
        response_text, resend = _parse_and_strip_resend(response_text)

        if "[HUMAN_HANDOFF]" in response_text:
            response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
            if not response_text:
                response_text = "I am transferring you to a member of our team who can help you further. Please hold."
            # Proactive (/direct) turns strip the marker but never
            # trigger — the operator is already here.
            if not proactive:
                await self._hand_off_instagram(db, account, customer, context, message_body)
            return response_text

        response_text, agreement = _parse_and_strip_agreement(response_text)
        if agreement:
            _mark_turn_outcome(turn_outcome, "markers_seen")
            if proactive:
                # A proactive turn carries no customer consent — Amara's
                # words go out, but no order is ever closed from them.
                logger.warning("AGREEMENT_REACHED in a proactive turn; sending words only, no close.")
                return response_text
            try:
                note = await self._close_instagram_deal(db, account, customer, agreement)
            except Exception as deal_err:
                # Amara already told the buyer it's a deal — don't let that
                # stand with nothing following (#37).
                (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                    f"Instagram deal not closed; handing off: {deal_err}"
                )
                await self._hand_off_instagram(db, account, customer, context, message_body)
                return _holding_reply(None)
            if note:
                _mark_turn_outcome(turn_outcome, "order_action")
                response_text = f"{response_text}\n\n{note}".strip()
        elif quote and not proactive:
            self._record_quote(db, account.user_id, quote, "instagram_dm", instagram_account_id=account.id)

        # Same payment-resend path as WhatsApp (see generate_response
        # step 3b) — only the order lookup keys, phone source, and close
        # call differ. Skipped when this turn already closed (handoff
        # early-returns above).
        if resend is not None and proactive:
            logger.warning("RESEND_PAYMENT in a proactive turn; sending words only, no re-fire.")
        elif resend is not None and not agreement:
            _mark_turn_outcome(turn_outcome, "markers_seen")
            live_order = (
                db.query(Order)
                .filter(
                    Order.instagram_account_id == account.id,
                    Order.instagram_customer_id == customer.id,
                    Order.status == OrderStatus.PENDING,
                    Order.payment_type == PaymentType.FULL,
                )
                .order_by(Order.created_at.desc())
                .first()
            )
            if live_order is not None and _hold_expired(live_order):
                live_order = None
            if live_order is not None and (
                live_order.product is None or live_order.customer is None
            ):
                logger.warning(
                    "Ignoring live IG RESEND_PAYMENT order %s: product or customer row is gone.",
                    live_order.id,
                )
                live_order = None
            if live_order is not None and resend.get("product_name"):
                if (
                    live_order.product.name.lower() != resend["product_name"].lower()
                    or abs((live_order.agreed_price or 0) - resend["price"]) > 0.01
                ):
                    logger.warning(
                        "IG RESEND_PAYMENT names %r at %s but live order %s is %r at %s; closing fresh.",
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
                    db,
                    live_order,
                    live_order.customer.phone_number,
                    live_order.customer.language,
                )
            elif live_order is not None:
                resend_note = await self._dispatch_payment(
                    db,
                    live_order,
                    live_order.product,
                    live_order.customer.phone_number,
                    PaymentType.FULL,
                    live_order.customer.language,
                )
            elif resend.get("product_name"):
                if resend.get("payment_type") == "installment":
                    logger.warning("Ignoring IG RESEND_PAYMENT for an installment deal; legs are Tunzaa-pushed.")
                elif not resend.get("phone"):
                    # Instagram never gives us the number — without it in
                    # the marker there is no phone to send the fresh prompt
                    # to. The model's words go out alone.
                    logger.warning("Ignoring IG RESEND_PAYMENT fallback close: marker carries no phone number.")
                else:
                    try:
                        resend_note = await self._close_instagram_deal(
                            db,
                            account,
                            customer,
                            {
                                "product_name": resend["product_name"],
                                "price": resend["price"],
                                "payment_type": "full",
                                "phone": resend["phone"],
                            },
                        )
                    except Exception as deal_err:
                        (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                            f"IG resend fallback close failed; handing off: {deal_err}"
                        )
                        await self._hand_off_instagram(db, account, customer, context, message_body)
                        return _holding_reply(None)
            else:
                logger.warning(
                    "Ignoring IG RESEND_PAYMENT: no live PENDING order for this chat and no deal terms to close fresh."
                )
            if resend_note:
                _mark_turn_outcome(turn_outcome, "order_action")
                response_text = f"{response_text}\n\n{resend_note}".strip()

        return response_text
