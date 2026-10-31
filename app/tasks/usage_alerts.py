import logging

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.ids import coerce_uuid
from app.core.provider import WhatsAppProvider
from app.core.security import normalize_phone
from app.db.models import User
from app.db.session import SessionLocal
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)


def _account_label(merchant: User) -> str:
    if merchant.accounts:
        return merchant.accounts[0].name
    if merchant.instagram_accounts:
        return f"@{merchant.instagram_accounts[0].username}"
    return merchant.username or merchant.phone_number or "your shop"


@celery_app.task
def send_usage_warning(merchant_id: str, percent: int):
    """
    Tells the merchant (and OPS_ALERT_PHONE) they've used `percent`% of
    their monthly Amara replies (#41). Template, not free text, and sent
    with the platform's own credentials — same reasons as the handoff and
    payment alerts: a merchant never has an open 24h session with the
    platform number. Warn-only; nothing here stops Amara replying.
    """
    db = SessionLocal()
    try:
        merchant_uuid = coerce_uuid(merchant_id)
        merchant = db.get(User, merchant_uuid) if merchant_uuid else None
        if not merchant:
            return "Merchant not found."

        from app.services.usage import effective_reply_limit

        total = effective_reply_limit(db, merchant) or settings.PLAN_REPLY_LIMIT
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": "account", "text": _account_label(merchant)},
                    {"type": "text", "parameter_name": "percent", "text": f"{percent}%"},
                    {"type": "text", "parameter_name": "total", "text": f"{total:,}"},
                ],
            }
        ]
        recipients = {
            normalize_phone(p)
            for p in (merchant.username or merchant.phone_number, settings.OPS_ALERT_PHONE)
            if p
        }
        provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
        for to in sorted(recipients):
            try:
                resp = _run_async(provider.send_template(
                    to,
                    settings.WHATSAPP_USAGE_ALERT_TEMPLATE_NAME,
                    language_code=settings.WHATSAPP_USAGE_ALERT_TEMPLATE_LANGUAGE,
                    components=components,
                ))
                if "error" in resp:
                    logger.error(f"Usage warning to {to} rejected: {resp}")
            except Exception as e:
                logger.error(f"Usage warning to {to} failed: {e}")
        return f"Sent to {len(recipients)}."
    finally:
        db.close()
