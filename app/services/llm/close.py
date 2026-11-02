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
from app.services.llm.helpers import DealRejected, _lang_text, _race_loser_note
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
        # Subscriptions have no units — quantity is meaningless for them
        # (the dashboard doesn't even ask for it), so neither the
        # out-of-stock backstop nor the atomic claim below applies (#89).
        if not product.is_subscription:
            if (product.quantity or 0) < 1:
                raise DealRejected(f"AGREEMENT_REACHED for '{product.name}' but it's out of stock")

        payment_type = (
            PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
        )

        # Duplicate-close guard — before the stock claim, so a re-close
        # neither reserves a unit nor dispatches a second payment (#101).
        reclose_note = llm_facade._reclose_guard_note(
            db, customer.id, product, Order.account_id == account.id, customer.language
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
        try:
            db.commit()
        except IntegrityError:
            # Lost the double-close race — a concurrent close for this
            # customer+product committed its PENDING order first and the
            # partial unique index rejected ours (#114). Same outcome as
            # the guard catching it, one message later.
            db.rollback()
            return _race_loser_note(db, customer.id, product, payment_type, customer.language)
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

        note = await self._dispatch_payment(
            db, order, product, normalized_from_phone, payment_type, customer.language
        )
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
        # Subscriptions have no units — see _close_deal (#89).
        if not product.is_subscription:
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

        # An Instagram prospect buying a platform plan becomes a merchant:
        # without a User row, activation can't sync their AI access and
        # they can't log into the dashboard (see
        # _resolve_merchant_for_platform_subscription). Ordinary shoppers
        # deliberately get no User — only a plan confers it (#89, #90).
        if product.is_platform_plan and not db.query(User).filter(User.username == normalized_phone).first():
            db.add(User(username=normalized_phone))
            db.commit()

        payment_type = (
            PaymentType.INSTALLMENT if agreement["payment_type"] == "installment" else PaymentType.FULL
        )

        # Duplicate-close guard — same as _close_deal (#101).
        reclose_note = llm_facade._reclose_guard_note(
            db, customer.id, product, Order.instagram_account_id == account.id, customer.language
        )
        if reclose_note:
            return reclose_note

        # Atomic stock claim — see _close_deal; a lost race rejects
        # exactly like the out-of-stock pre-check above (#54). Skipped
        # for subscriptions, which have no units (#89).
        if not product.is_subscription and not llm_facade._try_claim_stock(db, product):
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
        try:
            db.commit()
        except IntegrityError:
            # Same double-close race as _close_deal above (#114).
            db.rollback()
            return _race_loser_note(db, customer.id, product, payment_type, customer.language)
        db.refresh(order)

        self._create_subscription_if_needed(db, order, product, customer, ig_customer)

        self._schedule_reminders(order.id)

        return await self._dispatch_payment(
            db, order, product, normalized_phone, payment_type, customer.language
        )
