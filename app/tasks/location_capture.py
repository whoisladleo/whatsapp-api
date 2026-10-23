import asyncio
import logging

from app.core.celery_app import celery_app
from app.core.ids import coerce_uuid
from app.core.security import normalize_phone
from app.db.models import Customer, WhatsAppAccount
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task
def capture_customer_location(
    account_id: str,
    from_phone: str,
    latitude: float,
    longitude: float,
    name: str = None,
    address: str = None,
):
    """
    Records a customer-shared WhatsApp location as their delivery location.
    Not routed through process_inbound_message — this is a fulfillment
    signal, not merchant-command/AI-negotiation text, so it gets its own
    small task rather than being shoehorned into that one's routing.
    """
    logger.info(f"Capturing shared location for {from_phone}")
    db = SessionLocal()
    try:
        normalized = normalize_phone(from_phone)
        customer = db.query(Customer).filter(Customer.phone_number == normalized).first()
        if not customer:
            logger.info(f"No known customer for {from_phone}; nothing to attach the location to.")
            return "No matching customer."

        customer.delivery_latitude = latitude
        customer.delivery_longitude = longitude
        # Only overwrite the free-text address when WhatsApp itself supplied
        # a named place (the customer picked a saved/business location) —
        # for a raw pin drop there's no name/address to use, and we don't
        # want to clobber a real address with a formatted coordinate string.
        label = name or address
        if label:
            customer.delivery_address = label
        db.commit()

        account_uuid = coerce_uuid(account_id)
        if account_uuid is None:
            return "Location saved; no account to send a confirmation from."
        account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_uuid).first()
        if not account:
            return "Location saved; no account to send a confirmation from."

        _send_confirmation(db, account, normalized)
        return "Success"
    finally:
        db.close()


def _send_confirmation(db, account, to_phone: str) -> None:
    from app.core.provider import WhatsAppProvider
    from app.crud import message as crud_message
    from app.services import usage

    body = "📍 Got your location, thanks! We'll use it for delivery."
    provider = WhatsAppProvider(account.access_token, account.phone_number_id)
    try:
        resp = asyncio.run(provider.send_text(to_phone, body))
    except Exception as e:
        logger.error(f"Failed to send location confirmation: {e}")
        resp = None
    if usage.whatsapp_send_succeeded(resp):
        usage.record_reply(db, account.user_id, "whatsapp", account_id=account.id)

    crud_message.create_message(
        db,
        {
            "to_phone": to_phone,
            "type": "text",
            "body": body,
            "direction": "outbound",
            "status": "sent" if resp else "failed",
            "raw": resp or {},
        },
        account.id,
    )
