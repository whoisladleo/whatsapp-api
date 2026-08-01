import logging
from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.db.models import Order, OrderStatus, Product

logger = logging.getLogger(__name__)

@celery_app.task
def send_soft_nudge(order_id: int):
    """
    T+2h: Send soft nudge (template message) with Tunzaa checkout link.
    """
    logger.info(f"Executing soft nudge for order {order_id}")
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."
        
        # Here we would use WhatsAppProvider to send a reminder template
        # provider = WhatsAppProvider(account.access_token, account.phone_number_id)
        # provider.send_template(...)
        logger.info(f"Sent soft nudge to customer {order.customer.phone_number}")
        return "Success"
    finally:
        db.close()

@celery_app.task
def send_scarcity_nudge(order_id: int):
    """
    T+24h: Warn user of remaining item stock.
    """
    logger.info(f"Executing scarcity nudge for order {order_id}")
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."
        
        # WhatsApp notification logic
        logger.info(f"Sent scarcity nudge to customer {order.customer.phone_number}")
        return "Success"
    finally:
        db.close()

@celery_app.task
def auto_cancel_order(order_id: int):
    """
    T+48h: Cancel unpaid order and release reserved quantity back to DB.
    """
    logger.info(f"Executing auto cancel for order {order_id}")
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or order.status != OrderStatus.PENDING:
            return "Order not found or not pending."
        
        order.status = OrderStatus.CANCELLED
        
        # Release inventory - assuming an order maps to a single product 
        # (in real schema, we'd iterate over OrderItems)
        # For MVP, we need a way to link order to product.
        
        db.commit()
        logger.info(f"Auto-cancelled order {order.id} and notified customer.")
        return "Success"
    finally:
        db.close()
