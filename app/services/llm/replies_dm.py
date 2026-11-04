import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.models import (
    AmaraEventKind,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    Order,
    OrderStatus,
    PaymentType,
    Product,
)
from app.services import usage
from app.services.handoff_flow import consume_directive_ig, format_directive_block
from app.services.llm.constants import FAILED_CYCLES_HANDOFF_THRESHOLD
from app.services.llm.conversation import (
    CHANNEL_INSTAGRAM,
    build_merchant_prompt,
    build_platform_prompt,
    render_catalog,
)
from app.services.llm.helpers import DealRejected, _bump_quote_cycle
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

        # The customer just sent a new message, so they're no longer
        # "quiet" — proactively clear any outstanding quote-nudge state,
        # mirroring generate_response. Skipped for proactive (/direct)
        # turns, where no new inbound arrived.
        if context.pending_quote_at is not None and not proactive:
            context.pending_quote_price = None
            context.pending_quote_at = None
            context.quote_nudge_count = 0
            db.commit()

        # One-shot operator steering (/direct), consumed on this generated
        # reply — the handoff/quota callers return before reaching here,
        # so a paused conversation keeps its directive until Amara
        # actually generates. Appended to the system prompt below, never
        # to anything customer-facing.
        directive = consume_directive_ig(customer.id)

        # On the platform account the counterparty is a prospect merchant,
        # not a customer: the catalog is the Taskflow plans, mirroring
        # generate_response's platform branch.
        is_platform_account = account.is_platform_account
        if is_platform_account:
            from app.services.subscription_access import get_platform_plans

            products = [p for p in get_platform_plans(db) if not p.is_draft]
        else:
            # Drafts excluded — see the comment-reply builder above.
            products = (
                db.query(Product)
                .filter(Product.merchant_id == account.user_id, Product.is_draft == False)
                .all()
            )
        catalog_str = render_catalog(products)

        history_str = ""
        for m in conversation_history:
            sender = "Customer" if m["direction"] == "inbound" else "Amara"
            history_str += f"{sender}: {m['body']}\n"

        # One-shot classification, stored IG-side (see
        # InstagramCustomer.language) — notes go single-language once
        # known, bilingual until then.
        need_language_marker = customer.language is None
        directive_block = format_directive_block(directive) if directive else None
        if is_platform_account:
            outbound_count = db.query(InstagramDirectMessage).filter(
                InstagramDirectMessage.customer_id == customer.id,
                InstagramDirectMessage.direction == "outbound",
            ).count()
            instruction = build_platform_prompt(
                channel=CHANNEL_INSTAGRAM,
                catalog_str=catalog_str,
                is_first_message=(outbound_count == 0),
                multi_plan=len(products) > 1,
                knowledge_content=knowledge_content,
                directive_block=directive_block,
                need_language_marker=need_language_marker,
            )
        else:
            instruction = build_merchant_prompt(
                channel=CHANNEL_INSTAGRAM,
                account_name=account_name,
                catalog_str=catalog_str,
                knowledge_content=knowledge_content,
                directive_block=directive_block,
                need_language_marker=need_language_marker,
            )

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
            return _holding_reply(customer.language)
        except Exception as e:
            if retry_on_outage and _is_transient_llm_error(e):
                raise LLMUnavailable(str(e)) from e
            logger.error(f"DM-response LLM error; handing off: {e}")
            if proactive:
                return None
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _outage_reply(customer.language)

        if not (response_text or "").strip():
            # Empty even after the inline retry — don't end in silence:
            # hand off (the holding line promises a human follows up) and
            # send the same holding reply a failed close uses (#101).
            logger.warning("DM-response LLM empty after retry; handing off.")
            if proactive:
                return None
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _holding_reply(customer.language)

        # One-shot language classification (only requested in the prompt
        # while customer.language is still unset) — stripped first so it
        # can never leak, then persisted for single-language notes.
        response_text, detected_language = _parse_and_strip_language(response_text)
        if detected_language and customer.language is None:
            customer.language = detected_language
            db.commit()

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
                note = await self._close_instagram_deal(db, account, customer, agreement, context)
            except Exception as deal_err:
                # Amara already told the buyer it's a deal — don't let that
                # stand with nothing following (#37).
                (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                    f"Instagram deal not closed; handing off: {deal_err}"
                )
                await self._hand_off_instagram(db, account, customer, context, message_body)
                return _holding_reply(customer.language)
            if note:
                _mark_turn_outcome(turn_outcome, "order_action")
                response_text = f"{response_text}\n\n{note}".strip()
        elif quote and not proactive and not account.is_platform_account:
            await self._track_quote_ig(db, account, context, customer, quote, message_body)

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
            async def _close_fresh(marker_terms: dict) -> Optional[str]:
                return await self._close_instagram_deal(
                    db,
                    account,
                    customer,
                    {
                        "product_name": marker_terms["product_name"],
                        "price": marker_terms["price"],
                        "payment_type": "full",
                        "phone": marker_terms["phone"],
                    },
                    context,
                )

            try:
                resend_note = await self._resend_payment_note(
                    db,
                    live_order=live_order,
                    resend=resend,
                    phone_for_dispatch=(
                        live_order.customer.phone_number
                        if live_order is not None and live_order.customer is not None
                        else resend.get("phone")
                    ),
                    fallback_phone=resend.get("phone"),
                    language=customer.language,
                    close_fresh_fn=_close_fresh,
                )
            except Exception as deal_err:
                (logger.warning if isinstance(deal_err, DealRejected) else logger.error)(
                    f"IG resend fallback close failed; handing off: {deal_err}"
                )
                await self._hand_off_instagram(db, account, customer, context, message_body)
                return _holding_reply(customer.language)
            if resend_note:
                _mark_turn_outcome(turn_outcome, "order_action")
                response_text = f"{response_text}\n\n{resend_note}".strip()

        return response_text

    async def _track_quote_ig(
        self,
        db: Session,
        account: InstagramAccount,
        context: InstagramConversationContext,
        ig_customer: InstagramCustomer,
        quote: dict,
        message_body: str,
    ) -> None:
        """IG twin of _track_quote: records an outstanding [QUOTE: ...],
        counts consecutive rounds on the same product via the shared
        _bump_quote_cycle, and schedules the first "still interested?"
        nudge. Hits the same failed-cycle threshold into the IG handoff.
        A quote for an unknown/draft product isn't tracked (nothing
        sensible to nudge about)."""
        from app.tasks.quote_nudges import QUOTE_NUDGE_INTERVALS_SECONDS, send_quote_nudge

        product = (
            db.query(Product)
            .filter(
                Product.merchant_id == account.user_id,
                Product.name.ilike(quote["product_name"]),
                Product.is_draft == False,
            )
            .first()
        )
        if not product:
            logger.warning(f"QUOTE for unknown product '{quote['product_name']}'; not tracking a nudge.")
            return

        _bump_quote_cycle(context, product.name)
        context.pending_quote_price = quote["price"]
        context.pending_quote_at = datetime.utcnow()
        context.quote_nudge_count = 0
        db.commit()
        usage.record_event(
            db, account.user_id, AmaraEventKind.QUOTE, channel="instagram_dm",
            instagram_account_id=account.id, product_name=product.name, amount=quote["price"],
        )

        if (context.failed_cycles or 0) >= FAILED_CYCLES_HANDOFF_THRESHOLD:
            await self._hand_off_instagram(db, account, ig_customer, context, message_body)
            return

        try:
            send_quote_nudge.apply_async(
                args=[str(context.id), str(account.id), context.pending_quote_at.isoformat(), 1, "instagram_dm"],
                countdown=QUOTE_NUDGE_INTERVALS_SECONDS[0],
            )
        except Exception as sched_err:
            logger.error(f"Failed to schedule quote nudge for context {context.id}: {sched_err}")
