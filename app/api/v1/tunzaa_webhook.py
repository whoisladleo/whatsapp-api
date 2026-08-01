from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import Order, OrderStatus, Installment, InstallmentStatus, WhatsAppAccount
from app.core.provider import WhatsAppProvider
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/tunzaa-webhook")
async def handle_tunzaa_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Webhook endpoint to receive Tunzaa payments updates.
    Expects events like PAYMENT_COMPLETE or FIRST_INSTALLMENT_SUCCESS.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(f"Received Tunzaa Webhook: {data}")

    # Extract relevant info depending on actual Tunzaa schema
    event = data.get("event")
    order_ref = data.get("reference")
    
    if not order_ref:
        return {"status": "ignored", "reason": "No reference provided"}

    order = db.query(Order).filter(Order.id == int(order_ref.split("-")[-1])).first() if "-" in order_ref else None

    if not order:
        return {"status": "ignored", "reason": "Order not found"}

    if event in ["PAYMENT_COMPLETE", "FIRST_INSTALLMENT_SUCCESS"]:
        order.status = OrderStatus.PAID
        db.commit()

        # Update installments if applicable
        installment_id = data.get("installment_id")
        if installment_id:
            installment = db.query(Installment).filter(
                Installment.order_id == order.id,
                Installment.tunzaa_installment_id == installment_id
            ).first()
            if installment:
                installment.status = InstallmentStatus.PAID
                db.commit()

        # Trigger AI / WhatsApp notification for delivery address
        # We need the merchant account to send the message
        # In a real app we'd map order -> customer -> context -> account
        # Assuming the order has a product -> merchant -> account link.
        # But for MVP, let's just log it or send it if we can find the account.
        
        # We could query WhatsAppAccount by user_id of the merchant.
        # However, we'd need to link Order to Product/Merchant.
        # Currently Order only links to Customer. Let's assume we can fetch it if needed.
        
        return {"status": "success", "message": "Order marked as PAID"}
    
    elif event == "PAYMENT_FAILED":
        order.status = OrderStatus.FAILED
        db.commit()
        return {"status": "success", "message": "Order marked as FAILED"}

    return {"status": "ignored", "reason": "Unhandled event type"}
