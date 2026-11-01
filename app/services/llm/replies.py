import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import AmaraEventKind, ConversationContext, Product, WhatsAppAccount
from app.services import llm as llm_facade
from app.services import usage
from app.services.llm.constants import FAILED_CYCLES_HANDOFF_THRESHOLD
from app.services.llm.helpers import _is_out_of_stock
from app.services.llm.markers import _parse_comment_reply
from app.services.llm.providers import MuseProvider, _complete_with_retry

logger = logging.getLogger("app.services.llm")


class ReplyMixin:
    def _get_knowledge_base(self, product_slug: str) -> str:
        kb_path = f"knowledge/{product_slug}.md"
        if not os.path.exists(kb_path):
            return ""
        
        with open(kb_path, 'r') as f:
            return f.read()

    async def _track_quote(
        self,
        db: Session,
        account: WhatsAppAccount,
        context: ConversationContext,
        quote: dict,
        from_phone: str,
        message_body: str,
        is_platform_account: bool,
    ) -> None:
        """
        Records an outstanding [QUOTE: ...] and schedules the first
        "still interested?" nudge. Resolves the product name defensively
        the same way _close_deal does — a quote for a product that doesn't
        actually exist in the catalog isn't tracked (nothing sensible to
        nudge the customer about).

        Also implements Rule 3: failed_cycles counts consecutive quote
        rounds on the *same* product (i.e. the customer pushed back and got
        countered again) — a quote for a new/different product means a
        fresh negotiation and resets the count. Hitting the threshold
        force-escalates to HUMAN_HANDOFF even if the model itself never
        judged the customer "unreasonable" enough to emit [HUMAN_HANDOFF].
        """
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

        if context.current_product_intent == product.name:
            context.failed_cycles = (context.failed_cycles or 0) + 1
        else:
            context.failed_cycles = 0

        context.current_product_intent = product.name
        context.pending_quote_price = quote["price"]
        context.pending_quote_at = datetime.utcnow()
        context.quote_nudge_count = 0
        db.commit()
        usage.record_event(
            db, account.user_id, AmaraEventKind.QUOTE, channel="whatsapp",
            account_id=account.id, product_name=product.name, amount=quote["price"],
        )

        if context.failed_cycles >= FAILED_CYCLES_HANDOFF_THRESHOLD:
            await self._trigger_human_handoff(
                db, account, context, from_phone, message_body, is_platform_account
            )
            return

        try:
            send_quote_nudge.apply_async(
                args=[str(context.id), str(account.id), context.pending_quote_at.isoformat(), 1],
                countdown=QUOTE_NUDGE_INTERVALS_SECONDS[0],
            )
        except Exception as sched_err:
            logger.error(f"Failed to schedule quote nudge for context {context.id}: {sched_err}")

    async def generate_comment_reply(
        self,
        comment_body: str,
        merchant_id: uuid.UUID,
        db: Session,
        account_name: str = "the shop",
        knowledge_content: Optional[str] = None,
        post_media: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Generates Amara's response to a public Instagram comment: a short
        public reply (visible to everyone, never states a price) plus a
        one-shot private-reply DM with the actual details. Deliberately
        stateless — unlike generate_response, there's no ConversationContext
        or negotiation here. A private reply can only be sent once per
        comment (Meta's own limit), and there's no inbound-DM channel yet to
        continue a conversation over (see Issue 7 — full Amara-over-DM).
        Returns None (caller sends nothing) if the model is unavailable or
        its output doesn't match the expected PUBLIC:/PRIVATE: format.
        post_media is the parent post fetched by InstagramProvider.get_media
        (B1 threads it in; B2 matches its photo against the catalog on the
        Muse provider only, and names the pictured product in the reply).
        """
        if not self.client:
            return None

        # Drafts are invisible to Amara: excluded at the query, so the model
        # can neither offer nor match them (close paths reject them as a
        # backstop). Same filter on all three catalog builders.
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
                stock_note = " (OUT OF STOCK)" if _is_out_of_stock(p) else ""
                catalog_str += f"- {p.name}: Price TZS {p.max_price:,.0f}, Stock: {p.quantity}{stock_note}\n"
                if p.description:
                    catalog_str += f"  {p.description}\n"

        # Vision product-match (B2, Muse-only): when the commented-on post
        # has a usable photo, identify which catalog product it shows so
        # the reply below can name it. Any failure (no match, no media,
        # download/model error — or the Claude provider) leaves
        # matched_product None and the reply is exactly the old one.
        matched_product = None
        if post_media and isinstance(self.provider, MuseProvider):
            try:
                matched_product = await llm_facade.match_post_to_product(
                    client=self.client,
                    model=self.provider.model_id,
                    post_media=post_media,
                    products=products,
                )
            except Exception as e:
                logger.warning(f"Vision product-match failed: {e}")

        instruction = f"""You are Amara, the AI sales agent for {account_name}. A customer just left a public comment on one of the shop's Instagram posts:

"{comment_body}"

{catalog_str}
"""
        if matched_product is not None:
            instruction += (
                f"\nThe photo on the post they commented on shows this catalog item: "
                f"{matched_product.name} (Price TZS {matched_product.max_price:,.0f}). "
                f"The comment is almost certainly about it — name it in your PRIVATE reply "
                f"when relevant, with its catalog price exactly as given."
            )
        if knowledge_content:
            instruction += f"\nHere is additional knowledge:\n{knowledge_content}"

        instruction += """
Reply with EXACTLY two lines, nothing else, in this format:
PUBLIC: <a short, friendly public reply visible to everyone — never state a price or personal details, just acknowledge them and say you've sent more info to their DMs>
PRIVATE: <a short direct message with the actual details they're asking about, using only the catalog/knowledge above — never invent a product, price, stock level, or feature/detail that isn't there>

If the comment has nothing to do with the shop's products (off-topic, spam, greetings), still use the same two-line format — briefly deflect and invite them to ask about products.

Out of stock: never offer a product the catalog marks OUT OF STOCK — if the comment asks about one, first say plainly in your PRIVATE reply, naming the product, that it is out of stock, then suggest an in-stock alternative from the catalog instead.

All prices are in Tanzanian Shillings (TZS) — always state them exactly as given, including the TZS label. Never say "$", "USD", "dollars", or imply any other currency, and never convert the number into another currency.

Match the customer's language (Swahili or English). Keep both lines to one sentence each.
"""

        try:
            response_text = await _complete_with_retry(
                self.provider,
                system=instruction,
                messages=[{"role": "user", "content": "Respond as Amara:"}],
                # 300 truncated Muse on off-topic comments (reasoning
                # exhausted the budget, finish_reason=length, content=None
                # -> unparseable -> sends nothing). 500 matches the DM
                # budget; Claude replies are short so the higher cap
                # doesn't change its output. A length stop now retries
                # at a doubled cap inside the helper; a double cut
                # lands here and stays silent (#103).
                max_tokens=500,
            )
        except Exception as e:
            logger.error(f"Comment-reply LLM error: {e}")
            return None

        parsed = _parse_comment_reply(response_text)
        if not parsed:
            logger.warning(
                f"Comment-reply response didn't match PUBLIC/PRIVATE format: {response_text!r}"
            )
            return None

        public_reply, private_reply = parsed
        return {"public_reply": public_reply, "private_reply": private_reply}
