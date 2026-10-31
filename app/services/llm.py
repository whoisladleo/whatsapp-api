import anthropic
import logging
import math
import openai
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional
from app.core.attachments import describe_attachments
from app.core.config import settings
from app.core.security import normalize_phone
from sqlalchemy.orm import Session
from app.db.models import (
    Customer,
    ConversationContext,
    ConversationState,
    Installment,
    InstallmentStatus,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    WhatsAppAccount,
    Message,
    User,
    AmaraEventKind,
)
from app.services import usage
from app.services.vision_match import match_post_to_product

logger = logging.getLogger(__name__)

# How long a reservation (and the matching order_reminders schedule) holds
# stock before auto_cancel_order releases it — see app/tasks/order_reminders.py.
RESERVATION_WINDOW = timedelta(hours=48)

_AGREEMENT_RE = re.compile(
    r"\[AGREEMENT_REACHED:\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.]+)\s+payment=(?P<payment>full|installment)"
    r"(?:\s+phone=(?P<phone>\S+))?\s*\]",
    re.IGNORECASE,
)

_QUOTE_RE = re.compile(
    r"\[QUOTE:\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.]+)\s*\]",
    re.IGNORECASE,
)

_COMMENT_REPLY_RE = re.compile(
    r"PUBLIC:\s*(?P<public>.+?)\s*\n+\s*PRIVATE:\s*(?P<private>.+)",
    re.IGNORECASE | re.DOTALL,
)

# Only "en"/"sw" are real options — matches Customer.language's only
# consumers (WHATSAPP_OTP_TEMPLATE_LANGUAGE / WHATSAPP_REMINDER_TEMPLATE_LANGUAGE),
# and every customer-facing template on this account is registered under
# exactly these two codes (see CLAUDE.md).
_LANGUAGE_RE = re.compile(r"\[LANGUAGE:\s*(?P<language>en|sw)\s*\]", re.IGNORECASE)


def _parse_and_strip_language(text: str):
    """
    Extracts the one-shot [LANGUAGE: en|sw] marker generate_response asks
    the model to emit only while Customer.language is still unset (see the
    system prompt below) — the model already infers/matches the customer's
    language every turn per its existing VOICE instructions, this just
    captures that as structured data instead of only shaping prose. Always
    strips the marker from what the customer sees, whether or not it
    parses.
    """
    match = _LANGUAGE_RE.search(text)
    if not match:
        return text, None
    cleaned = _LANGUAGE_RE.sub("", text).strip()
    return cleaned, match.group("language").lower()


def _parse_comment_reply(text: str):
    """
    Parses the PUBLIC:/PRIVATE: two-line format generate_comment_reply asks
    the model for. Returns None on any malformed/empty output rather than
    guessing — the caller falls back to not sending anything sooner than
    risk sending a broken or empty message publicly.
    """
    match = _COMMENT_REPLY_RE.search(text)
    if not match:
        return None
    public_reply = match.group("public").strip()
    private_reply = match.group("private").strip()
    if not public_reply or not private_reply:
        return None
    return public_reply, private_reply

# Rule 3 (README/CLAUDE.md): "after 3 failed price agreement attempts...
# switch context to HUMAN_HANDOFF". ConversationContext.failed_cycles counts
# consecutive [QUOTE] rounds on the same product (see _track_quote) — hitting
# this many force-escalates regardless of what the model itself judges.
FAILED_CYCLES_HANDOFF_THRESHOLD = 3

# Handoff alerts quote the customer's own message so the merchant has
# something to actually respond to (see _trigger_human_handoff /
# _notify_merchant_of_instagram_handoff) — bounded so an unusually long
# customer message can't blow up the alert or exceed the handoff_notification
# template's {{message}} variable's practical length.
HANDOFF_ALERT_MESSAGE_MAX_CHARS = 200


def _truncate_for_alert(text: str, max_chars: int = HANDOFF_ALERT_MESSAGE_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _parse_and_strip_quote(text: str):
    """
    Extracts the [QUOTE: product=... price=...] marker the model is
    instructed to emit whenever it states a specific price during
    negotiation but hasn't closed the deal yet — used to schedule a
    "still interested?" follow-up if the customer goes quiet (see
    app/tasks/quote_nudges.py). Same parse-and-always-strip pattern as
    _parse_and_strip_agreement.
    """
    match = _QUOTE_RE.search(text)
    if not match:
        return text, None

    cleaned = _QUOTE_RE.sub("", text).strip()

    try:
        price = float(match.group("price"))
    except ValueError:
        return cleaned, None

    product_name = match.group("product").strip()
    if not product_name:
        return cleaned, None

    return cleaned, {"product_name": product_name, "price": price}


def _parse_and_strip_agreement(text: str):
    """
    Extracts the [AGREEMENT_REACHED: product=... price=... payment=...]
    marker the model is instructed to emit once a deal closes (regular-
    merchant branch only — see the system prompt below), the same way
    [HUMAN_HANDOFF] is parsed. Always strips the marker from what the
    customer sees, whether or not it parses; a malformed marker just means
    no order gets created.

    The optional `phone=` group only ever appears in Instagram DM output
    (see generate_dm_response's prompt) — the WhatsApp flow already knows
    the customer's phone number from the conversation itself, so its prompt
    never asks the model to include one, and `match.group("phone")` is
    simply None there.
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
        "phone": match.group("phone"),
    }

# The model only ever sees a floor 10% above the real min_price, so whatever
# it leaks or concedes in free text is still above what the merchant
# approved. _close_deal/_close_instagram_deal still enforce the real
# min_price as the hard guarantee (#37).
NEGOTIATION_FLOOR_BUFFER = 1.10


def _negotiation_floor(product: Product) -> float:
    return min(product.max_price, math.ceil(product.min_price * NEGOTIATION_FLOOR_BUFFER))


def _is_out_of_stock(product: Product) -> bool:
    """
    Whether the catalog should show this product as OUT OF STOCK: no units
    left (quantity < 1, e.g. via /out_of_stock) or deactivated by the
    merchant (is_active False). Either way the model must not offer it —
    see the out-of-stock RULES line in each prompt. Server-side
    _close_deal/_close_instagram_deal still reject a quantity-0 close as
    the backstop (#53).
    """
    return (product.quantity or 0) < 1 or product.is_active is False


def _try_claim_stock(db: Session, product: Product) -> bool:
    """
    Atomically claims one unit of stock via a conditional UPDATE
    (SET quantity = quantity - 1 WHERE id = ... AND quantity >= 1) —
    the same claim style as claim_available_pool_number: the
    affected-row count tells us whether we won the race, correct on
    both SQLite tests and Postgres prod (#54). Returns True when this
    call took the unit; False (0 rows) means a concurrent close took
    the last unit first, and the caller must reject the deal exactly
    like the out-of-stock pre-check. Refreshes the ORM object on
    success so callers see the post-claim quantity.
    """
    claimed = (
        db.query(Product)
        .filter(Product.id == product.id, Product.quantity >= 1)
        # synchronize_session=False: the SET value is a SQL expression
        # (quantity - 1), not a literal, so there is no Python-side value
        # to copy onto in-session objects — refresh below instead.
        .update({Product.quantity: Product.quantity - 1}, synchronize_session=False)
    )
    db.commit()
    if claimed == 0:
        return False
    db.refresh(product)
    if (product.quantity or 0) < 1:
        # The claim just took the last unit, so the catalog's availability
        # flipped to out of stock — sync it. Non-zero claims change no
        # catalog field (quantity itself is never sent), so they sync nothing.
        from app.tasks.catalog_sync import dispatch_catalog_sync
        dispatch_catalog_sync(product.id, "upsert")
    return True


class DealRejected(Exception):
    """An AGREEMENT_REACHED marker failed server-side validation; no order was created."""


_HOLDING_REPLIES = {
    "sw": "Asante! Naomba nithibitishe hili na duka kwanza — mtu wa timu yetu atakujibu hapa hivi punde.",
    "en": "Thanks! Let me confirm this with the shop first — someone from our team will reply here shortly.",
}


def _holding_reply(language: Optional[str]) -> str:
    """
    Replaces Amara's reply when a deal she just agreed to can't be closed —
    otherwise the buyer has been told a price and nothing follows (#37).
    Both languages when the customer's language isn't known (Instagram
    never classifies it).
    """
    if language in _HOLDING_REPLIES:
        return _HOLDING_REPLIES[language]
    return f"{_HOLDING_REPLIES['sw']}\n\n{_HOLDING_REPLIES['en']}"


class LLMUnavailable(Exception):
    """
    A transient LLM failure (connection error, 429, 5xx) that survived
    the SDK's own retries, raised only when the caller asked to retry
    (retry_on_outage=True). Raised before any order, send or state change
    for this turn, so the caller can safely re-run the whole turn (#38).
    """


def _is_transient_llm_error(e: Exception) -> bool:
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError)):
        return True
    if isinstance(e, (anthropic.APIStatusError, openai.APIStatusError)):
        return e.status_code == 429 or e.status_code >= 500
    return False


_OUTAGE_REPLIES = {
    "sw": "Samahani, nina tatizo la kiufundi kwa sasa — mtu wa timu yetu atakujibu hapa hivi punde.",
    "en": "Sorry, I'm having a technical problem right now — someone from our team will reply here shortly.",
}


def _outage_reply(language: Optional[str]) -> str:
    """Sent once Claude retries are exhausted; the chat is handed to the merchant at the same time (#38)."""
    if language in _OUTAGE_REPLIES:
        return _OUTAGE_REPLIES[language]
    return f"{_OUTAGE_REPLIES['sw']}\n\n{_OUTAGE_REPLIES['en']}"


class LLMProvider:
    """
    One model backend behind LLMService. complete() takes the same shape
    every caller already builds (a system instruction, a user-messages
    list, a max_tokens cap) and returns the reply as plain text.
    """

    name = ""
    model_id = ""

    def __init__(self, client=None):
        self.client = client

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        raise NotImplementedError


class ClaudeProvider(LLMProvider):
    """Existing behavior, unchanged: Anthropic messages API, same call args."""

    name = "claude"
    model_id = "claude-sonnet-5"

    def __init__(self, api_key: Optional[str]):
        super().__init__(
            client=anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        )

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        response = await self.client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        # This model returns extended-thinking content blocks before the
        # text block non-deterministically (confirmed live — the same
        # prompt sometimes returns [ThinkingBlock, TextBlock] and
        # sometimes just [TextBlock]), so content[0] isn't reliably the
        # reply — filter for the actual text block(s) instead.
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


class MuseProvider(LLMProvider):
    """Meta's Muse API, reached through the OpenAI-compatible SDK."""

    name = "muse"
    model_id = "muse-spark-1.3"
    base_url = "https://api.meta.ai/v1"

    def __init__(self, api_key: Optional[str]):
        super().__init__(
            client=openai.AsyncOpenAI(api_key=api_key, base_url=self.base_url)
            if api_key
            else None
        )

    async def complete(self, *, system: str, messages: list, max_tokens: int) -> str:
        response = await self.client.chat.completions.create(
            model=self.model_id,
            max_tokens=max_tokens,
            # Negotiation replies don't need deep reasoning; at the default
            # effort the model spends the whole budget on reasoning tokens
            # and returns content=None with finish_reason=length (#49).
            reasoning_effort="minimal",
            messages=[{"role": "system", "content": system}, *messages],
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        if not content:
            logger.warning(
                "Muse returned empty content (finish_reason=%s)",
                choice.finish_reason,
            )
        return content


class LLMService:
    def __init__(self, api_key: str, provider: Optional[str] = None):
        provider = provider or settings.LLM_PROVIDER
        if provider == MuseProvider.name:
            self.provider: LLMProvider = MuseProvider(api_key=api_key)
        elif provider == ClaudeProvider.name:
            self.provider = ClaudeProvider(api_key=api_key)
        else:
            logger.warning(
                "Unknown LLM_PROVIDER=%r — falling back to 'claude'", provider
            )
            self.provider = ClaudeProvider(api_key=api_key)

    @property
    def client(self):
        """The underlying SDK client (None when unconfigured)."""
        return self.provider.client

    @client.setter
    def client(self, value):
        self.provider.client = value

    @property
    def model_id(self):
        return self.provider.model_id

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
        note to append to the customer-facing reply. Raises DealRejected for
        an invalid agreement — the caller replaces the model's reply with a
        holding line and hands off, since the buyer was just told a deal
        was agreed (#37).
        """
        product = (
            db.query(Product)
            .filter(Product.merchant_id == account.user_id, Product.name.ilike(agreement["product_name"]))
            .first()
        )
        price = agreement["price"]
        if not product:
            raise DealRejected(f"AGREEMENT_REACHED for unknown product '{agreement['product_name']}'")
        # Drafts carry placeholder 0 prices — a close must never land on
        # one, even if a merchant set stock on it before completing it.
        if product.is_draft:
            raise DealRejected(f"AGREEMENT_REACHED for '{product.name}' but it's an uncompleted draft")
        if not (product.min_price <= price <= product.max_price):
            raise DealRejected(
                f"AGREEMENT_REACHED price {price} outside [{product.min_price}, {product.max_price}] "
                f"for '{product.name}' (possible prompt injection)"
            )
        if (product.quantity or 0) < 1:
            raise DealRejected(f"AGREEMENT_REACHED for '{product.name}' but it's out of stock")

        payment_type = (
            PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
        )

        # Reserve: claim one unit atomically (conditional UPDATE, not a
        # read-modify-write) so two concurrent closes on the last unit
        # can't both succeed — the loser sees 0 rows and is rejected
        # exactly like the out-of-stock pre-check above (#54). The claim
        # is still now, not deferred to payment success (soft-lock during
        # the hold window, so a second customer can't be sold the same
        # unit while this one is paying).
        if not _try_claim_stock(db, product):
            raise DealRejected(f"AGREEMENT_REACHED for '{product.name}' but it's out of stock")
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

        self._create_subscription_if_needed(db, order, product, customer)

        context.last_offer = price
        # No longer just a quote — it's a real order now, so nothing to
        # nudge the customer about, and the failed-attempts count resets.
        context.pending_quote_price = None
        context.pending_quote_at = None
        context.quote_nudge_count = 0
        context.failed_cycles = 0
        db.commit()

        self._schedule_reminders(order.id)

        note = await self._dispatch_payment(db, order, product, normalized_from_phone, payment_type)
        return note

    def _create_subscription_if_needed(
        self,
        db: Session,
        order: Order,
        product: Product,
        customer: Customer,
        instagram_customer: Optional[InstagramCustomer] = None,
    ) -> None:
        """
        For a Product with is_subscription=True, creates (or reuses an
        existing unpaid one — see product_subscription.get_or_create_pending_subscription)
        the ProductSubscription that turns this order's first charge into
        recurring billing (see app/services/product_subscription.py). The
        first charge itself still rides the normal one-time
        Order/_dispatch_payment flow below; app/api/v1/tunzaa_webhook.py
        activates this row once that first payment is confirmed.
        instagram_customer is only passed for a deal closed over Instagram
        DM — it's what lets renewal-failure notices go out as a DM instead
        of a WhatsApp message nobody would receive.
        """
        if not product.is_subscription:
            return

        from app.services import product_subscription

        product_subscription.get_or_create_pending_subscription(
            db, order, product, customer.id,
            instagram_customer_id=instagram_customer.id if instagram_customer else None,
        )

    async def _dispatch_payment(
        self, db: Session, order: Order, product: Product, phone: str, payment_type: PaymentType
    ) -> str:
        from app.services.tunzaa import tunzaa_service

        # WhatsApp's location-sharing feature has no Instagram equivalent we
        # actually process — InstagramWebhookParser.parse_inbound_messages
        # only ever extracts message.text, there's no location-message
        # handling for that channel at all (unlike WhatsApp's
        # capture_customer_location task). Telling an Instagram customer to
        # use a feature we can't read would be actively misleading, so this
        # asks for a plain text address there instead.
        location_note = (
            "reply here with your delivery address so we know where to deliver it."
            if order.instagram_account_id
            else "share your delivery location using WhatsApp's location-sharing feature (the 📎 "
            "attach icon) so we know where to deliver it."
        )

        try:
            if payment_type == PaymentType.FULL:
                resp = await tunzaa_service.initiate_payment(
                    phone=phone, amount=order.agreed_price, reference=f"order-{order.id}"
                )
                order.tunzaa_transaction_id = resp.get("transactionID")
                db.commit()
                return (
                    f"A payment request for TZS {order.agreed_price:,.0f} has been sent to your "
                    f"phone — please approve it to complete your order. While you're at it, please {location_note}"
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
                    f"details. Please also {location_note}"
                )
        except Exception as e:
            logger.error(f"Tunzaa dispatch failed for order {order.id}: {e}")
            # This order never got as far as an accepted payment attempt —
            # release the reservation rather than leave it dangling.
            product.quantity += order.quantity or 1
            order.status = OrderStatus.CANCELLED
            db.commit()
            # The release may flip availability back to in stock (including
            # undoing the out-of-stock sync the claim above queued).
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(product.id, "upsert")
            return "Something went wrong setting up your payment — please try again in a moment."

    def _schedule_reminders(self, order_id: uuid.UUID) -> None:
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
                task.apply_async(args=[str(order_id)], countdown=delay)
            except Exception as sched_err:
                logger.error(f"Failed to schedule {task.name}: {sched_err}")

    async def _trigger_human_handoff(
        self,
        db: Session,
        account: WhatsAppAccount,
        context: ConversationContext,
        from_phone: str,
        customer_message: str,
        is_platform_account: bool,
    ) -> None:
        """
        Marks the conversation HUMAN_HANDOFF and alerts the merchant.
        Shared by both handoff triggers: the model's own [HUMAN_HANDOFF]
        marker (tone-based judgment) and the hard failed_cycles counter
        (Rule 3 — see FAILED_CYCLES_HANDOFF_THRESHOLD). Does not touch this
        turn's customer-facing response_text; callers decide what to send.
        customer_message is what the *customer* said, not Amara's own
        reply — without it the merchant has nothing to actually respond to
        when they use /reply. Threaded through into the handoff_notification
        template as its {{message}} variable (see _send_platform_alert_to_merchant)
        — the template was updated in WhatsApp Manager specifically to add
        this slot, rather than dropping the customer's message from the
        alert once alerts moved off free text.
        """
        context.state = ConversationState.HUMAN_HANDOFF
        context.handoff_at = datetime.utcnow()
        db.commit()
        usage.record_event(db, account.user_id, AmaraEventKind.HANDOFF, channel="whatsapp", account_id=account.id)

        if is_platform_account or not account.owner:
            return

        await self._send_platform_alert_to_merchant(
            db,
            account.owner,
            from_phone,
            customer_message,
            alert_target={
                "channel": "whatsapp",
                "ref": from_phone,
                "name": from_phone,
                "account_id": str(account.id),
                "customer_id": None,
            },
        )

        from app.tasks.handoff_reminders import (
            send_handoff_reminder,
            HANDOFF_REMINDER_DELAY_SECONDS,
        )
        try:
            send_handoff_reminder.apply_async(
                args=[str(context.id)], countdown=HANDOFF_REMINDER_DELAY_SECONDS
            )
        except Exception as sched_err:
            logger.error(f"Failed to schedule handoff reminder: {sched_err}")

    async def _hand_off_instagram(
        self, db: Session, account: InstagramAccount, customer: InstagramCustomer,
        context: InstagramConversationContext, customer_message: str,
    ) -> None:
        context.state = ConversationState.HUMAN_HANDOFF
        context.handoff_at = datetime.utcnow()
        db.commit()
        usage.record_event(
            db, account.user_id, AmaraEventKind.HANDOFF, channel="instagram_dm", instagram_account_id=account.id
        )
        await self._notify_merchant_of_instagram_handoff(db, account, customer, customer_message)

    def _record_quote(self, db: Session, merchant_id, quote: dict, channel: str, **account_ids) -> None:
        """Ledger entry for GET /stats' products discussed (#41). Unknown products aren't recorded."""
        product = (
            db.query(Product)
            .filter(Product.merchant_id == merchant_id, Product.name.ilike(quote["product_name"]))
            .first()
        )
        if product:
            usage.record_event(
                db, merchant_id, AmaraEventKind.QUOTE, channel=channel,
                product_name=product.name, amount=quote["price"], **account_ids,
            )

    async def _notify_merchant_of_instagram_handoff(
        self, db: Session, ig_account: InstagramAccount, customer: InstagramCustomer, customer_message: str
    ) -> None:
        """
        Instagram has no way to message the account owner directly — only
        customers who've messaged the connected account (see CLAUDE.md's
        Instagram section on notify_merchant's identical limitation for
        subscription-renewal alerts) — so a DM handoff is surfaced via
        WhatsApp instead, the same cross-channel pattern
        app/services/product_subscription.py's notify_merchant already
        uses. Sent via the platform's own default WhatsApp credentials
        (see _send_platform_alert_to_merchant) — the merchant's personal
        number is how they talk to Amara/Taskflow itself, independent of
        whether they've connected their own business WhatsAppAccount.
        Unlike _trigger_human_handoff's WhatsApp-native equivalent, this
        doesn't also schedule a 15-minute reminder nudge — that task
        resolves the account to alert from the customer's last *WhatsApp*
        message, which has no equivalent for an Instagram-originated
        handoff. customer_message is what the *customer* said, not
        Amara's own reply — without it the merchant has nothing to
        actually respond to when they use /reply.
        """
        merchant = ig_account.owner
        if not merchant:
            return

        # /reply's Instagram path matches on username OR ig_scoped_id (see
        # _relay_to_instagram_customer), so this identifier is always
        # something the merchant can actually paste back in, even before
        # the display-name backfill has ever run for a brand-new customer.
        reply_identifier = customer.username or customer.ig_scoped_id
        await self._send_platform_alert_to_merchant(
            db,
            merchant,
            f"@{reply_identifier}",
            customer_message,
            alert_target={
                "channel": "instagram",
                "ref": reply_identifier,
                "name": f"@{reply_identifier}",
                "account_id": None,
                "customer_id": str(customer.id),
            },
        )

    async def _send_platform_alert_to_merchant(
        self,
        db: Session,
        merchant: User,
        reply_identifier: str,
        customer_message: str,
        alert_target: dict | None = None,
    ) -> None:
        """
        Sends a handoff alert to the merchant's own personal WhatsApp
        number via the approved handoff_notification template
        (WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME), using the platform's own
        default credentials — the same ones send_otp_message uses — rather
        than requiring the merchant's own business WhatsAppAccount.

        A template send is required here, not free text: WhatsApp only
        allows free-form messages to a recipient with an open 24h session
        (i.e. someone who's messaged this number back recently), and a
        merchant essentially never has — they only ever *receive* from the
        platform's number. This was a real production incident: the old
        free-text send got a 200 OK from Meta's API on every attempt, yet
        never actually reached the merchant's phone, silently dropped as
        an out-of-window re-engagement message. Templates are exempt from
        that restriction.

        handoff_notification has a header ("A customer {{customer}} needs
        your help right now.") and a body ("They said: \"{{message}}\" /
        Reply directly using: /reply {{customer}} \"your message\"") —
        {{customer}} appears in BOTH, so it must be supplied in both the
        header and body components' parameters, not just once (get this
        wrong and Meta rejects the whole send, the same way the OTP
        button sub_type mismatch did). reply_identifier is what the
        merchant should paste into /reply — a bare phone number for a
        WhatsApp-native handoff, or "@username"/"@ig_scoped_id" for an
        Instagram one (see call sites). customer_message is truncated to
        HANDOFF_ALERT_MESSAGE_MAX_CHARS for the template's {{message}}
        variable.

        Best-effort/silent if the merchant has no phone on file at all
        (checks username, then the Facebook-login self-reported
        phone_number — same resolution order
        subscription_access.find_platform_subscription uses).
        """
        merchant_phone = merchant.username or merchant.phone_number
        if not merchant_phone:
            return

        from app.core.config import settings
        from app.core.provider import WhatsAppProvider

        components = [
            {
                "type": "header",
                "parameters": [
                    {"type": "text", "parameter_name": "customer", "text": reply_identifier},
                ],
            },
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": "message", "text": _truncate_for_alert(customer_message)},
                    {"type": "text", "parameter_name": "customer", "text": reply_identifier},
                ],
            },
        ]

        provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
        try:
            alert_resp = await provider.send_template(
                merchant_phone,
                settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME,
                language_code=settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_LANGUAGE,
                components=components,
            )
        except Exception as alert_err:
            logger.error(f"Failed to send platform alert to merchant: {alert_err}")
            alert_resp = None

        # Maps the sent alert to its customer so a button tap's context id
        # resolves to the exact conversation (template button payloads are
        # static — see app/services/handoff_flow.py).
        if alert_target is not None and alert_resp and "messages" in alert_resp:
            from app.services.handoff_flow import remember_alert

            wamid = (alert_resp["messages"][0] or {}).get("id")
            if wamid:
                remember_alert(wamid, alert_target)

        # Only log this as a Message if the platform's own number is
        # actually connected as a real WhatsAppAccount (see CLAUDE.md) —
        # until then there's no account row to attach it to, and the send
        # itself still succeeds/fails independent of that being logged.
        platform_account = (
            db.query(WhatsAppAccount)
            .filter(WhatsAppAccount.phone_number_id == settings.PHONE_NUMBER_ID)
            .first()
        )
        if not platform_account:
            return

        from app.crud import message as crud_message
        crud_message.create_message(
            db,
            {
                "to_phone": merchant_phone,
                "type": "template",
                "body": f"{settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME}: {reply_identifier} — {_truncate_for_alert(customer_message)}",
                "direction": "outbound",
                "status": "sent" if alert_resp else "failed",
                "raw": alert_resp or {},
            },
            platform_account.id,
        )

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
                matched_product = await match_post_to_product(
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
            response_text = await self.provider.complete(
                system=instruction,
                messages=[{"role": "user", "content": "Respond as Amara:"}],
                # 300 truncated Muse on off-topic comments (reasoning
                # exhausted the budget, finish_reason=length, content=None
                # -> unparseable -> sends nothing). 500 matches the DM
                # budget; Claude replies are short so the higher cap
                # doesn't change its output.
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

    async def _close_instagram_deal(
        self, db: Session, account: InstagramAccount, ig_customer: InstagramCustomer, agreement: dict
    ) -> Optional[str]:
        """
        Instagram counterpart to _close_deal: validates the parsed
        AGREEMENT_REACHED marker the same way (server-side catalog/price/
        stock enforcement), then reserves stock and creates the Order —
        except there's no pre-existing WhatsApp Customer/ConversationContext
        to work from here, since Instagram never gave us a phone number up
        front. The model is instructed to ask for one before emitting this
        marker (see generate_dm_response's prompt); `agreement["phone"]` is
        that number. Once normalized, it resolves/creates the same
        phone-keyed Customer the WhatsApp flow uses, so payment dispatch,
        reminders, and the dashboard Orders page all work identically
        regardless of which channel the deal originated on — the only
        difference is Order.instagram_account_id instead of .account_id.
        ig_customer (the InstagramCustomer, distinct from the phone-keyed
        Customer above) is only needed to let a subscription product's
        renewal notices go out as an Instagram DM later — see
        _create_subscription_if_needed.
        """
        product = (
            db.query(Product)
            .filter(Product.merchant_id == account.user_id, Product.name.ilike(agreement["product_name"]))
            .first()
        )
        price = agreement["price"]
        if not product:
            raise DealRejected(f"Instagram AGREEMENT_REACHED for unknown product '{agreement['product_name']}'")
        # Same draft backstop as the WhatsApp close path above.
        if product.is_draft:
            raise DealRejected(f"Instagram AGREEMENT_REACHED for '{product.name}' but it's an uncompleted draft")
        if not (product.min_price <= price <= product.max_price):
            raise DealRejected(
                f"Instagram AGREEMENT_REACHED price {price} outside [{product.min_price}, {product.max_price}] "
                f"for '{product.name}' (possible prompt injection)"
            )
        if (product.quantity or 0) < 1:
            raise DealRejected(f"Instagram AGREEMENT_REACHED for '{product.name}' but it's out of stock")

        phone = agreement.get("phone")
        if not phone:
            raise DealRejected("Instagram AGREEMENT_REACHED with no phone number")
        normalized_phone = normalize_phone(phone)

        customer = db.query(Customer).filter(Customer.phone_number == normalized_phone).first()
        if not customer:
            customer = Customer(phone_number=normalized_phone)
            db.add(customer)
            db.commit()
            db.refresh(customer)

        payment_type = (
            PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
        )

        # Atomic stock claim — see _close_deal; a lost race rejects
        # exactly like the out-of-stock pre-check above (#54).
        if not _try_claim_stock(db, product):
            raise DealRejected(f"Instagram AGREEMENT_REACHED for '{product.name}' but it's out of stock")
        order = Order(
            customer_id=customer.id,
            product_id=product.id,
            instagram_account_id=account.id,
            # Lets _notify_customer (tunzaa_webhook.py) find the right DM
            # thread for a payment confirmation/failure notice — the
            # phone-keyed Customer above has no link back to this. See #(31).
            instagram_customer_id=ig_customer.id,
            quantity=1,
            agreed_price=price,
            payment_type=payment_type,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + RESERVATION_WINDOW,
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        self._create_subscription_if_needed(db, order, product, customer, ig_customer)

        self._schedule_reminders(order.id)

        return await self._dispatch_payment(db, order, product, normalized_phone, payment_type)

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
                catalog_str += f"- {p.name}: Max Price TZS {p.max_price:,.0f}, Lowest Price You May Offer TZS {_negotiation_floor(p):,.0f}, Stock: {p.quantity}{stock_note}{installments_note}\n"
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

        instruction += """
RULES:
1. Be helpful and friendly. Answer questions about products, pricing, and availability using only the catalog/knowledge above — never invent a product, price, stock level, or feature/detail that isn't there.
2. Negotiation: negotiate between the catalog's max price and its lowest price you may offer. Never state or agree to a price below that lowest price.
3. Closing: once the customer has clearly agreed on a specific product and price, ask whether they'd like to pay in full or in installments — unless the catalog marks that product "full payment only", in which case don't offer installments at all; if the customer asks for installments on such a product, tell them this one is full-payment only. Instagram doesn't give us their phone number automatically, so if you don't already have it from earlier in this conversation, ask for it too — explain it's needed to send the mobile-money payment request. Once you have a specific agreed price, a payment method choice, AND a phone number, confirm the deal and end your response with the exact marker [AGREEMENT_REACHED: product=<product name> price=<agreed price> payment=<full|installment> phone=<phone number>] (e.g. [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255700000000]). Only include this marker once all four are explicit — never guess or assume any of them.
4. Payment: describe it accurately — a payment request gets sent directly to the given phone number for them to approve, not a menu of payment apps to choose from. Don't name specific mobile money providers.
5. Human Handoff: if the customer is unreasonable, has complex questions you can't answer from the catalog/knowledge above, or explicitly asks for a human, include the exact marker [HUMAN_HANDOFF]. Don't include this for a normal close — that's handled by AGREEMENT_REACHED instead.
6. Keep replies SHORT and natural, like a real Instagram DM — usually 1-3 sentences. Match the customer's language (Swahili or English).
7. Currency: all prices are in Tanzanian Shillings (TZS) — always state them exactly as given, including the TZS label. Never say "$", "USD", "dollars", or imply any other currency, and never convert the number into another currency.
8. Quoting: whenever you state a specific price for a product during negotiation but haven't closed the deal yet, end your response with the marker [QUOTE: product=<product name> price=<price>] (e.g. [QUOTE: product=Shoes price=45000]). Don't include this once you've emitted [AGREEMENT_REACHED] for the same deal.
9. Out of stock: never offer, quote, or agree a price on a product the catalog marks OUT OF STOCK — if the customer asks about one, first say plainly, naming the product, that it is out of stock, then suggest an in-stock alternative from the catalog instead.

Reply with ONLY the message text (plus whichever marker applies, if any) — no other formatting.
"""

        try:
            response_text = await self.provider.complete(
                system=instruction,
                messages=[
                    {
                        "role": "user",
                        "content": f'Recent conversation:\n{history_str}\nCustomer just sent: "{message_body}"\n\nRespond as Amara:',
                    }
                ],
                max_tokens=500,
            )
        except Exception as e:
            if retry_on_outage and _is_transient_llm_error(e):
                raise LLMUnavailable(str(e)) from e
            logger.error(f"DM-response LLM error; handing off: {e}")
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return _outage_reply(None)

        # Stripped up front so the marker can never leak through the early
        # handoff return below.
        response_text, quote = _parse_and_strip_quote(response_text)

        if "[HUMAN_HANDOFF]" in response_text:
            response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
            if not response_text:
                response_text = "I am transferring you to a member of our team who can help you further. Please hold."
            await self._hand_off_instagram(db, account, customer, context, message_body)
            return response_text

        response_text, agreement = _parse_and_strip_agreement(response_text)
        if agreement:
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
                response_text = f"{response_text}\n\n{note}".strip()
        elif quote:
            self._record_quote(db, account.user_id, quote, "instagram_dm", instagram_account_id=account.id)

        return response_text

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

        # The customer just sent a new message, so they're no longer
        # "quiet" — proactively clear any outstanding quote-nudge state
        # regardless of what (if anything) this turn's reply ends up being.
        # This is the main defense against a stale nudge firing later; the
        # nudge task itself also re-checks validity at fire time as a
        # second layer (see app/tasks/quote_nudges.py).
        if context.pending_quote_at is not None:
            context.pending_quote_price = None
            context.pending_quote_at = None
            context.quote_nudge_count = 0
            db.commit()

        # If already in handoff, don't generate AI response
        if context.state == ConversationState.HUMAN_HANDOFF:
            # We assume a human agent is dealing with it. 
            return None # Don't send an automated message

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

        # 2. Fetch products for this merchant (drafts excluded — see above)
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
                catalog_str += f"- {p.name}: Max Price TZS {p.max_price:,.0f}, Lowest Price You May Offer TZS {_negotiation_floor(p):,.0f}, Stock: {p.quantity}{stock_note}{installments_note}\n"
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
- The most persuasive thing you can say is that you bring real money, not just conversation. Every time you make the case for trying you, explicitly say you close sales and get payment moving — not just that you answer messages fast. Use words like "pesa"/"mauzo"/"order halisi" (or "money"/"sales"/"real orders" in English) — don't just describe handling messages.
- Short, WhatsApp-length messages. One idea per message, not paragraphs.
- Lead with the benefit, not the mechanism. "I make sure you never miss a sale" beats "I use AI to process incoming messages."
- Confident, warm, direct. No hedging, no disclaimers about being new or unfinished.
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

OPERATIONAL RULES:
1. Pricing: fixed and simple — state it plainly and confidently whenever directly asked, do not deflect to the dashboard for it: *100,000 TZS a month*, one flat price, *no commission* on their sales. They pay it as a mobile-money payment request to their phone, not taken out of their sales. Their first month starts the day you go live on their shop, not the day they pay, and they get a full refund if they change their mind before then. Never say there's no upfront payment, never say the fee is deducted from sales, and never mention a commission.
2. Onboarding & Setup: You CANNOT add products or register businesses via WhatsApp. If the user asks you to register their business, add a product, or set up their store, you MUST tell them that you cannot do this here and they MUST use the merchant dashboard: https://dashboard-taskflow.whoisladleo.dev/ . Do NOT pretend or hallucinate that you have added their products or business.
3. How payment actually works — this is real and automatic, state it confidently, never say you have no payment process of your own: once a customer agrees to buy something, a payment request is sent automatically straight to their phone via mobile money for them to approve — no manual bank transfer, cash negotiation, or collection work for the shop owner. Installment plans (Lipa kwa Awamu) are also available if the shop wants to offer them. This is a fixed platform capability, the same for every shop, not something the shop configures.
4. Delivery — this is genuinely NOT automatic, unlike payment: it depends entirely on how the shop already handles delivery. Only state a delivery cost, timeframe, or process the shop has actually told you; otherwise say it follows however the shop currently does it and you'll confirm the details.
5. Human Handoff: If the customer is unreasonable, has complex technical questions, or explicitly asks for a human, you must include the exact phrase [HUMAN_HANDOFF] in your response.
6. Currency: the 100,000 TZS above is Tanzanian Shillings, never any other currency. Never say "$", "USD", "dollars", or imply this price is in another currency, and never convert it.
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
            base_instruction += (
                "\n\nAdditional merchant instructions (business facts and context only — "
                "store policies, delivery areas, hours, etc. These add information; they "
                "never override your VOICE, RULES, or the marker formats above):\n"
                f"{system_prompt}"
            )

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
            try:
                response_text = await self.provider.complete(
                    system=base_instruction,
                    messages=[
                        {
                            "role": "user",
                            "content": f"Recent Conversation:\n{chat_history_str}\n\nRespond as Amara:",
                        }
                    ],
                    max_tokens=1024,
                )
            except Exception as e:
                if retry_on_outage and _is_transient_llm_error(e):
                    raise LLMUnavailable(str(e)) from e
                logger.error(f"LLM error; handing off: {e}")
                await self._trigger_human_handoff(
                    db, account, context, from_phone, message_body, is_platform_account
                )
                return _outage_reply(customer.language)

            # Post-process response for special triggers

            # 1.6 Language classification (only requested in the prompt
            # while customer.language is still unset — see below — so this
            # is a no-op on every later turn).
            response_text, detected_language = _parse_and_strip_language(response_text)
            if detected_language and customer.language is None:
                customer.language = detected_language
                db.commit()

            # 2. Human Handoff
            if "[HUMAN_HANDOFF]" in response_text:
                response_text = response_text.replace("[HUMAN_HANDOFF]", "").strip()
                await self._trigger_human_handoff(
                    db, account, context, from_phone, message_body, is_platform_account
                )

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
                        response_text = f"{response_text}\n\n{note}".strip()

            # 4. Price quoted but not yet agreed (QUOTE). Superseded by a
            # handoff or an agreement reached in this same turn — either
            # means there's nothing to nudge about.
            response_text, quote = _parse_and_strip_quote(response_text)
            if quote and not agreement and not is_platform_account and context.state != ConversationState.HUMAN_HANDOFF:
                await self._track_quote(
                    db, account, context, quote, from_phone, message_body, is_platform_account
                )

            return response_text
        except LLMUnavailable:
            raise
        except Exception as e:
            print(f"LLM Error: {e}")
            return "I'm sorry, I'm having trouble processing your request right now."

def _default_llm_service() -> Optional[LLMService]:
    if settings.LLM_PROVIDER == MuseProvider.name:
        return LLMService(api_key=settings.MODEL_API_KEY, provider="muse") if settings.MODEL_API_KEY else None
    return LLMService(api_key=settings.ANTHROPIC_API_KEY) if settings.ANTHROPIC_API_KEY else None


llm_service = _default_llm_service()
