import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import normalize_phone
from app.db.models import (
    ConversationContext,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Installment,
    InstallmentStatus,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.services import llm as llm_facade
from app.services.llm.constants import RESERVATION_WINDOW
from app.services.llm.conversation import CHANNEL_INSTAGRAM, CHANNEL_WHATSAPP
from app.services.llm.helpers import DealRejected, _hold_expired, _lang_text, _race_loser_note
# _reclose_guard_note and _try_claim_stock are called via llm_facade (not
# imported) so tests patching app.services.llm.* keep landing. Same for
# match_post_to_product in replies.py.
from app.services.llm.prompts import (
    _DELIVERY_JOINERS,
    _INSTALLMENT_LINES,
    _LOCATION_NOTES,
    _PAYMENT_DISPATCH_FAILED_LINES,
    _PAYMENT_REQUEST_LINES,
    _PAYMENT_RESEND_LINES,
    _PAYMENT_RESEND_THROTTLED_LINES,
)

logger = logging.getLogger("app.services.llm")


def _validated_close_target(db: Session, merchant_id, agreement: dict, label: str) -> tuple:
    """Shared AGREEMENT_REACHED validation for both close paths: the product
    must exist, be completed, and the price must sit inside its band with
    stock to sell (subscriptions skip the stock rule — they have no units,
    #89). label prefixes the rejection reason with the channel (""/"Instagram
    ") so logs keep attributing the path. Raises DealRejected — the caller
    replaces the model's reply with the holding line and hands off (#37)."""
    product = (
        db.query(Product)
        .filter(Product.merchant_id == merchant_id, Product.name.ilike(agreement["product_name"]))
        .first()
    )
    price = agreement["price"]
    if not product:
        raise DealRejected(f"{label}AGREEMENT_REACHED for unknown product '{agreement['product_name']}'")
    # Drafts carry placeholder 0 prices — a close must never land on one,
    # even if a merchant set stock on it before completing it.
    if product.is_draft:
        raise DealRejected(f"{label}AGREEMENT_REACHED for '{product.name}' but it's an uncompleted draft")
    if not (product.min_price <= price <= product.max_price):
        raise DealRejected(
            f"{label}AGREEMENT_REACHED price {price} outside [{product.min_price}, {product.max_price}] "
            f"for '{product.name}' (possible prompt injection)"
        )
    if not product.is_subscription:
        if (product.quantity or 0) < 1:
            raise DealRejected(f"{label}AGREEMENT_REACHED for '{product.name}' but it's out of stock")
    payment_type = (
        PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
    )
    return product, payment_type


class CloseMixin:
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
        WhatsApp close: validates the parsed AGREEMENT_REACHED marker
        server-side (the prompt's floor instruction alone doesn't stop a
        prompt-injected model from claiming an out-of-band price) and runs
        the shared close tail. Returns a short note to append to the
        customer-facing reply. Raises DealRejected for an invalid agreement
        — the caller replaces the model's reply with a holding line and
        hands off, since the buyer was just told a deal was agreed (#37).
        """
        product, payment_type = _validated_close_target(db, account.user_id, agreement, label="")
        return await self._close_core(
            db,
            customer=customer,
            product=product,
            price=agreement["price"],
            payment_type=payment_type,
            channel=CHANNEL_WHATSAPP,
            account_id=account.id,
            language=customer.language,
            phone_for_dispatch=normalized_from_phone,
            context=context,
        )

    async def _close_core(
        self,
        db: Session,
        *,
        customer: Customer,
        product: Product,
        price: float,
        payment_type: PaymentType,
        channel: str,
        account_id=None,
        instagram_account_id=None,
        instagram_customer: Optional[InstagramCustomer] = None,
        language: Optional[str],
        phone_for_dispatch: str,
        context=None,
        label: str = "",
    ) -> Optional[str]:
        """Shared deal-closing tail for both channels: duplicate guard,
        atomic stock claim, order row, subscription link, quote-state reset,
        reminders, and the Tunzaa dispatch. context is either context model
        (both carry the same quote fields now) or None. Raises DealRejected
        exactly like the old per-channel closes."""
        channel_filter = (
            Order.account_id == account_id
            if channel == CHANNEL_WHATSAPP
            else Order.instagram_account_id == instagram_account_id
        )
        # Duplicate-close guard — before the stock claim, so a re-close
        # neither reserves a unit nor dispatches a second payment (#101).
        reclose_note = llm_facade._reclose_guard_note(
            db, customer.id, product, channel_filter, language
        )
        if reclose_note:
            return reclose_note

        # Reserve: claim one unit atomically (conditional UPDATE, not a
        # read-modify-write) so two concurrent closes on the last unit
        # can't both succeed — the loser sees 0 rows and is rejected
        # exactly like the out-of-stock pre-check above (#54). The claim
        # is still now, not deferred to payment success (soft-lock during
        # the hold window, so a second customer can't be sold the same
        # unit while this one is paying).
        if not product.is_subscription and not llm_facade._try_claim_stock(db, product):
            raise DealRejected(f"{label}AGREEMENT_REACHED for '{product.name}' but it's out of stock")

        order = Order(
            customer_id=customer.id,
            product_id=product.id,
            account_id=account_id,
            instagram_account_id=instagram_account_id,
            instagram_customer_id=instagram_customer.id if instagram_customer else None,
            quantity=1,
            agreed_price=price,
            payment_type=payment_type,
            status=OrderStatus.PENDING,
            reserved_until=datetime.utcnow() + RESERVATION_WINDOW,
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            # Lost the double-close race — a concurrent close for this
            # customer+product committed its PENDING order first and the
            # partial unique index rejected ours (#114). Same outcome as
            # the guard catching it, one message later.
            db.rollback()
            return _race_loser_note(db, customer.id, product, payment_type, language)
        db.refresh(order)

        self._create_subscription_if_needed(db, order, product, customer, instagram_customer)

        if context is not None:
            context.last_offer = price
            # No longer just a quote — it's a real order now, so nothing to
            # nudge the customer about, and the failed-attempts count resets.
            context.pending_quote_price = None
            context.pending_quote_at = None
            context.quote_nudge_count = 0
            context.failed_cycles = 0
            db.commit()

        self._schedule_reminders(order.id)

        return await self._dispatch_payment(
            db, order, product, phone_for_dispatch, payment_type, language
        )

    async def _resend_payment_note(
        self,
        db: Session,
        *,
        live_order: Optional[Order],
        resend: dict,
        phone_for_dispatch: Optional[str],
        fallback_phone: Optional[str],
        language: Optional[str],
        close_fresh_fn,
    ) -> Optional[str]:
        """Shared RESEND_PAYMENT resolution for both channels. live_order is
        this chat's newest PENDING FULL order (looked up by the caller with
        channel keys) or None. Re-fires against it, dispatches onto it when
        the first attempt never went out, or closes fresh from the marker's
        terms when no live order exists. Returns the note to append, or None
        when there's nothing to resend. close_fresh_fn(resend) runs the
        channel's close and may raise — the caller hands off on failure."""
        if live_order is not None and _hold_expired(live_order):
            # A weeks-old hold, not this deal — the fallback below closes
            # fresh (and the reclose guard retires this row when it does).
            live_order = None
        if live_order is not None and (
            live_order.product is None or live_order.customer is None
        ):
            logger.warning(
                "Ignoring live RESEND_PAYMENT order %s: product or customer row is gone.",
                live_order.id,
            )
            live_order = None
        if live_order is not None and resend.get("product_name"):
            # The marker names a different deal than the live order
            # (abandoned cart for A, now closing B) — re-firing the wrong
            # prompt would charge for the wrong thing, so fall through to
            # a fresh close for the named deal instead.
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
        if live_order is not None and live_order.tunzaa_transaction_id:
            return await self._resend_payment_for_order(
                db, live_order, phone_for_dispatch, language
            )
        if live_order is not None:
            return await self._dispatch_payment(
                db, live_order, live_order.product, phone_for_dispatch, PaymentType.FULL, language
            )
        if resend.get("product_name"):
            if resend.get("payment_type") == "installment":
                # Plan legs are pushed on Tunzaa's own schedule — re-firing
                # would rebill a leg. The model's own words go out alone.
                logger.warning("Ignoring RESEND_PAYMENT for an installment deal; legs are Tunzaa-pushed.")
                return None
            if not fallback_phone:
                # Instagram never gives us the number — without it in the
                # marker there is no phone to send the fresh prompt to.
                logger.warning("Ignoring RESEND_PAYMENT fallback close: marker carries no phone number.")
                return None
            return await close_fresh_fn(resend)
        logger.warning(
            "Ignoring RESEND_PAYMENT: no live PENDING order for this chat and no deal terms to close fresh."
        )
        return None

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
        self, db: Session, order: Order, product: Product, phone: str, payment_type: PaymentType,
        language: Optional[str] = None,
    ) -> str:
        from app.services.tunzaa import tunzaa_service

        # WhatsApp's location-sharing feature has no Instagram equivalent we
        # actually process — InstagramWebhookParser.parse_inbound_messages
        # only ever extracts message.text, there's no location-message
        # handling for that channel at all (unlike WhatsApp's
        # capture_customer_location task). Telling an Instagram customer to
        # use a feature we can't read would be actively misleading, so this
        # asks for a plain text address there instead. Subscriptions skip
        # the ask entirely — there is nothing to deliver (#101).
        channel = "instagram" if order.instagram_account_id else "whatsapp"
        price = f"{order.agreed_price:,.0f}"

        def _with_delivery(line: str, joiner_key: str) -> str:
            if product.is_subscription:
                return line
            joiner = _lang_text(_DELIVERY_JOINERS[joiner_key], language)
            location = _lang_text(_LOCATION_NOTES[channel], language)
            if language in ("sw", "en"):
                return f"{line} {joiner} {location}."
            # Unknown language: each half is already a sw/en stack, so
            # join them per-language to keep the message readable.
            sw_line, en_line = line.split("\n\n")
            sw_loc, en_loc = location.split("\n\n")
            sw_join, en_join = joiner.split("\n\n")
            return f"{sw_line} {sw_join} {sw_loc}.\n\n{en_line} {en_join} {en_loc}."

        try:
            if payment_type == PaymentType.FULL:
                resp = await tunzaa_service.initiate_payment(
                    phone=phone, amount=order.agreed_price, reference=f"order-{order.id}"
                )
                # HTTP success alone means nothing — without a transaction
                # id no prompt went out, so this is a failed dispatch, not
                # a sent one (same guard as pay_now; the except below
                # cancels the order, releases stock, and reports failure).
                transaction_id = resp.get("transactionID")
                if not transaction_id:
                    raise RuntimeError(f"Tunzaa dispatch returned no transaction id for order {order.id}: {resp}")
                order.tunzaa_transaction_id = transaction_id
                db.commit()
                return _with_delivery(
                    _lang_text(_PAYMENT_REQUEST_LINES, language).format(price=price), "full"
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
                return _with_delivery(
                    _lang_text(_INSTALLMENT_LINES, language), "installment"
                )
        except Exception as e:
            logger.error(f"Tunzaa dispatch failed for order {order.id}: {e}")
            # This order never got as far as an accepted payment attempt —
            # release the reservation rather than leave it dangling. The
            # claim above is skipped for subscriptions (no units), so there
            # is nothing to release for them — restoring would mint phantom
            # stock (#101).
            if not product.is_subscription:
                product.quantity = (product.quantity or 0) + (order.quantity or 1)
            order.status = OrderStatus.CANCELLED
            db.commit()
            # The release may flip availability back to in stock (including
            # undoing the out-of-stock sync the claim above queued).
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(product.id, "upsert")
            return _lang_text(_PAYMENT_DISPATCH_FAILED_LINES, language)

    async def _resend_payment_for_order(
        self, db: Session, order: Order, phone: str, language: Optional[str]
    ) -> Optional[str]:
        """
        Re-sends the Tunzaa prompt for an already-placed PENDING order
        whose buyer says the request never arrived ([RESEND_PAYMENT]).
        Same order-<id> reference, so an approval of the old prompt or
        the new one completes the same order; the newest transaction id
        wins. FULL orders only — installment legs are pushed per Tunzaa's
        own schedule, not ours — and throttled (see RESEND_MIN_INTERVAL).
        Returns the note to append, or None when there's nothing to
        resend (the model's own words go out alone).
        """
        from app.services.payment_resend import ResendOutcome, resend_live_order

        if order.payment_type != PaymentType.FULL or not order.tunzaa_transaction_id:
            logger.warning(
                f"Ignoring RESEND_PAYMENT for order {order.id}: not a dispatched FULL order."
            )
            return None
        # The Tunzaa call, throttle, and transaction bookkeeping live in
        # the shared core now (#117) — this path only renders the note.
        result = await resend_live_order(db, order, phone)
        if result.outcome is ResendOutcome.THROTTLED:
            return _lang_text(_PAYMENT_RESEND_THROTTLED_LINES, language)
        if result.outcome is ResendOutcome.RESENT:
            return _lang_text(_PAYMENT_RESEND_LINES, language)
        if result.outcome is ResendOutcome.TUNZAA_ERROR:
            return _lang_text(_PAYMENT_DISPATCH_FAILED_LINES, language)
        return None  # installment_refused: unreachable (pre-checked above), fail silent

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

    async def _close_instagram_deal(
        self,
        db: Session,
        account: InstagramAccount,
        ig_customer: InstagramCustomer,
        agreement: dict,
        context: Optional[InstagramConversationContext] = None,
    ) -> Optional[str]:
        """
        Instagram close: same server-side validation as _close_deal, except
        there's no pre-existing WhatsApp Customer to work from, since
        Instagram never gave us a phone number up front. The model is
        instructed to ask for one before emitting this marker (see the DM
        prompt); `agreement["phone"]` is that number. Once normalized, it
        resolves/creates the same phone-keyed Customer the WhatsApp flow
        uses, so payment dispatch, reminders, and the dashboard Orders page
        all work identically regardless of which channel the deal originated
        on — the only difference is Order.instagram_account_id instead of
        .account_id. ig_customer (the InstagramCustomer, distinct from the
        phone-keyed Customer above) is only needed to let a subscription
        product's renewal notices go out as an Instagram DM later — see
        _create_subscription_if_needed.
        """
        product, payment_type = _validated_close_target(
            db, account.user_id, agreement, label="Instagram "
        )

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
        if customer.language is None and ig_customer.language is not None:
            # First writer wins across channels (see Customer.language): an
            # IG classification backfills a still-unset phone row so later
            # template sends pick the right language.
            customer.language = ig_customer.language
            db.commit()

        # An Instagram prospect buying a platform plan becomes a merchant:
        # without a User row, activation can't sync their AI access and
        # they can't log into the dashboard (see
        # _resolve_merchant_for_platform_subscription). Ordinary shoppers
        # deliberately get no User — only a plan confers it (#89, #90).
        if product.is_platform_plan and not db.query(User).filter(User.username == normalized_phone).first():
            db.add(User(username=normalized_phone))
            db.commit()

        return await self._close_core(
            db,
            customer=customer,
            product=product,
            price=agreement["price"],
            payment_type=payment_type,
            channel=CHANNEL_INSTAGRAM,
            instagram_account_id=account.id,
            instagram_customer=ig_customer,
            language=ig_customer.language or customer.language,
            phone_for_dispatch=normalized_phone,
            context=context,
            label="Instagram ",
        )
