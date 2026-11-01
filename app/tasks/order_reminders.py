import asyncio
import logging
from datetime import datetime
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.ids import coerce_uuid
from app.db.session import SessionLocal
from app.db.models import (
    Order,
    OrderStatus,
    Product,
    ProductSubscription,
    ProductSubscriptionStatus,
    WhatsAppAccount,
)
from app.crud import message as crud_message
from app.services import usage

logger = logging.getLogger(__name__)

# Scheduled once at order-creation time (see LLMService._schedule_reminders
# in app/services/llm.py). reserved_until/AUTO_CANCEL_DELAY_SECONDS share the
# same 48h window — reservation and hard-cancel deadline are the same moment.
SOFT_NUDGE_DELAY_SECONDS = 2 * 60 * 60
SCARCITY_NUDGE_DELAY_SECONDS = 24 * 60 * 60
AUTO_CANCEL_DELAY_SECONDS = 48 * 60 * 60


def _send_sync(provider, to, body=None, template_name=None, template_language=None, template_components=None):
    """
    Celery tasks run outside an event loop (a real worker has none in its
    thread), so a plain asyncio.run() is enough here — unlike
    app/tasks/webhook_processing.py's _run_async, these tasks are never
    invoked eagerly from within an already-running loop in tests (their
    apply_async calls are stubbed to no-ops there; task bodies are called
    directly, synchronously, in a plain test function).
    """
    if template_name:
        return asyncio.run(
            provider.send_template(
                to, template_name, language_code=template_language, components=template_components
            )
        )
    return asyncio.run(provider.send_text(to, body))


def _order_reminder_components(order) -> list:
    product_name = order.product.name if order.product else "your order"
    formatted_price = f"{order.agreed_price:,.0f}" if order.agreed_price is not None else "0"
    return [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": product_name},
                {"type": "text", "text": formatted_price},
            ],
        }
    ]


def _account_for_order(db, order) -> WhatsAppAccount:
    if not order.account_id:
        return None
    return db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()


def _log_outbound(db, account, to, body, resp):
    # Every caller is a buyer-facing reminder, so each successful one is billable (#41).
    if usage.whatsapp_send_succeeded(resp):
        usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)
    crud_message.create_message(
        db,
        {
            "to_phone": to,
            "type": "text",
            "body": body,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )


@celery_app.task
def send_soft_nudge(order_id: str):
    """
    T+2h: nudge the customer that their payment is still pending. Likely
    still within WhatsApp's 24h customer-service window (they were just
    negotiating), so a free-form text is used rather than a template.
    """
    logger.info(f"Executing soft nudge for order {order_id}")
    db = SessionLocal()
    try:
        order_uuid = coerce_uuid(order_id)
        order = db.query(Order).filter(Order.id == order_uuid).first() if order_uuid else None
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."

        account = _account_for_order(db, order)
        if not account or not order.customer:
            return "No account or customer to notify."

        from app.core.provider import WhatsAppProvider

        body = (
            "Just a reminder — your order is still waiting on payment. "
            "Approve the request on your phone whenever you're ready, or "
            "message us if you need help."
        )
        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        try:
            resp = _send_sync(provider, order.customer.phone_number, body=body)
        except Exception as e:
            logger.error(f"Soft nudge send failed for order {order_id}: {e}")
            resp = None
        _log_outbound(db, account, order.customer.phone_number, body, resp)

        logger.info(f"Sent soft nudge to customer {order.customer.phone_number}")
        return "Success"
    finally:
        db.close()


@celery_app.task
def send_scarcity_nudge(order_id: str):
    """
    T+24h: likely outside the 24h session window by now, so this uses a
    template message rather than free-form text (see
    settings.WHATSAPP_REMINDER_TEMPLATE_NAME — defaults to Meta's always-
    approved sample template; production use needs a real approved template
    for this account).
    """
    logger.info(f"Executing scarcity nudge for order {order_id}")
    db = SessionLocal()
    try:
        order_uuid = coerce_uuid(order_id)
        order = db.query(Order).filter(Order.id == order_uuid).first() if order_uuid else None
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."

        account = _account_for_order(db, order)
        if not account or not order.customer:
            return "No account or customer to notify."

        from app.core.provider import WhatsAppProvider

        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        try:
            resp = _send_sync(
                provider,
                order.customer.phone_number,
                template_name=settings.WHATSAPP_REMINDER_TEMPLATE_NAME,
                # order_reminder is registered in both "en" and "sw" under
                # the same template name — Customer.language (classified
                # once by the model itself, see llm.py's [LANGUAGE: ..]
                # marker) picks which variant to send, falling back to the
                # configured default while still unclassified.
                template_language=order.customer.language or settings.WHATSAPP_REMINDER_TEMPLATE_LANGUAGE,
                template_components=_order_reminder_components(order),
            )
        except Exception as e:
            logger.error(f"Scarcity nudge send failed for order {order_id}: {e}")
            resp = None
        _log_outbound(
            db,
            account,
            order.customer.phone_number,
            f"[template: {settings.WHATSAPP_REMINDER_TEMPLATE_NAME}] scarcity nudge",
            resp,
        )

        logger.info(f"Sent scarcity nudge to customer {order.customer.phone_number}")
        return "Success"
    finally:
        db.close()


@celery_app.task
def auto_cancel_order(order_id: str):
    """
    T+48h: cancel an unpaid order and release the reserved quantity back to
    Product.quantity (stock is decremented at reservation time, not
    deferred to payment success — see LLMService._close_deal). Also fails
    the linked subscription attempt, if any — an abandoned first payment
    must resolve to PAYMENT_FAILED, never sit in PENDING_PAYMENT granting
    access forever (#110).
    """
    logger.info(f"Executing auto cancel for order {order_id}")
    db = SessionLocal()
    try:
        order_uuid = coerce_uuid(order_id)
        order = db.query(Order).filter(Order.id == order_uuid).first() if order_uuid else None
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."
        if order.reserved_until is not None and order.reserved_until > datetime.utcnow():
            # A newer attempt refreshed the reservation after this firing
            # was scheduled (Pay Now reschedules on reuse) — this one is
            # stale; the newer firing will cancel at the new deadline.
            return "Reservation still active."

        order.status = OrderStatus.CANCELLED

        restored_product_id = None
        if order.product_id:
            product = db.query(Product).filter(Product.id == order.product_id).first()
            if product:
                product.quantity += order.quantity or 1
                restored_product_id = product.id

        db.commit()

        linked = (
            db.query(ProductSubscription)
            .filter(
                ProductSubscription.order_id == order.id,
                ProductSubscription.status == ProductSubscriptionStatus.PENDING_PAYMENT,
            )
            .first()
        )
        if linked is not None:
            from app.services import product_subscription
            product_subscription.mark_first_payment_failed(db, linked)

        if restored_product_id:
            # The release may flip the catalog's availability back to in stock.
            from app.tasks.catalog_sync import dispatch_catalog_sync
            dispatch_catalog_sync(restored_product_id, "upsert")

        account = _account_for_order(db, order)
        if account and order.customer:
            from app.core.provider import WhatsAppProvider

            body = (
                "Your reservation has expired since we didn't receive payment "
                "in time. The item has been released, but message us again "
                "any time if you'd still like it."
            )
            provider = WhatsAppProvider(account.access_token, account.phone_number_id)
            try:
                resp = _send_sync(provider, order.customer.phone_number, body=body)
            except Exception as e:
                logger.error(f"Auto-cancel notice send failed for order {order_id}: {e}")
                resp = None
            _log_outbound(db, account, order.customer.phone_number, body, resp)

        logger.info(f"Auto-cancelled order {order.id} and notified customer.")
        return "Success"
    finally:
        db.close()
