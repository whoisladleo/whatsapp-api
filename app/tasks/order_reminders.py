import asyncio
import logging
from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import SessionLocal
from app.db.models import Order, OrderStatus, Product, WhatsAppAccount
from app.crud import message as crud_message

logger = logging.getLogger(__name__)

# Scheduled once at order-creation time (see LLMService._schedule_reminders
# in app/services/llm.py). reserved_until/AUTO_CANCEL_DELAY_SECONDS share the
# same 48h window — reservation and hard-cancel deadline are the same moment.
SOFT_NUDGE_DELAY_SECONDS = 2 * 60 * 60
SCARCITY_NUDGE_DELAY_SECONDS = 24 * 60 * 60
AUTO_CANCEL_DELAY_SECONDS = 48 * 60 * 60


def _send_sync(provider, to, body=None, template_name=None):
    """
    Celery tasks run outside an event loop (a real worker has none in its
    thread), so a plain asyncio.run() is enough here — unlike
    app/tasks/webhook_processing.py's _run_async, these tasks are never
    invoked eagerly from within an already-running loop in tests (their
    apply_async calls are stubbed to no-ops there; task bodies are called
    directly, synchronously, in a plain test function).
    """
    if template_name:
        return asyncio.run(provider.send_template(to, template_name))
    return asyncio.run(provider.send_text(to, body))


def _account_for_order(db, order) -> WhatsAppAccount:
    if not order.account_id:
        return None
    return db.query(WhatsAppAccount).filter(WhatsAppAccount.id == order.account_id).first()


def _log_outbound(db, account, to, body, resp):
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
def send_soft_nudge(order_id: int):
    """
    T+2h: nudge the customer that their payment is still pending. Likely
    still within WhatsApp's 24h customer-service window (they were just
    negotiating), so a free-form text is used rather than a template.
    """
    logger.info(f"Executing soft nudge for order {order_id}")
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
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
def send_scarcity_nudge(order_id: int):
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
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."

        account = _account_for_order(db, order)
        if not account or not order.customer:
            return "No account or customer to notify."

        from app.core.provider import WhatsAppProvider

        provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        try:
            resp = _send_sync(
                provider, order.customer.phone_number, template_name=settings.WHATSAPP_REMINDER_TEMPLATE_NAME
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
def auto_cancel_order(order_id: int):
    """
    T+48h: cancel an unpaid order and release the reserved quantity back to
    Product.quantity (stock is decremented at reservation time, not
    deferred to payment success — see LLMService._close_deal).
    """
    logger.info(f"Executing auto cancel for order {order_id}")
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."

        order.status = OrderStatus.CANCELLED

        if order.product_id:
            product = db.query(Product).filter(Product.id == order.product_id).first()
            if product:
                product.quantity += order.quantity or 1

        db.commit()

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
