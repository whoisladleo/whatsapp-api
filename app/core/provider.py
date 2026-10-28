import logging
import httpx
from typing import Optional, Dict, Any, Union
from app.core.config import settings

logger = logging.getLogger(__name__)


class WhatsAppProvider:
    def __init__(self, access_token: str, phone_number_id: str):
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.base_url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def send_template(
        self,
        to: str,
        name: str,
        language_code: str = "en_US",
        components: Optional[list] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language_code},
            },
        }
        if components:
            payload["template"]["components"] = components

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def send_text(
        self,
        to: str,
        body: str,
        preview_url: bool = False,
        context_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        if context_message_id:
            # Renders as a quoted reply to that message in the customer's
            # WhatsApp — Meta requires a real wamid from this same number.
            payload["context"] = {"message_id": context_message_id}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def send_product_message(
        self,
        to: str,
        catalog_id: str,
        retailer_id: str,
        body: str,
        footer: Optional[str] = None,
        context_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Single-product card (Meta "product" interactive message). The card
        renders the catalog item's image/name/price; body is the required
        shop message shown with it, footer an optional grey line under it.
        retailer_id is str(Product.id) — see product_to_catalog_item.
        Raises nothing Meta-side itself; callers that need fallback use
        send_product_card_or_text instead.
        """
        url = f"{self.base_url}/messages"
        interactive: Dict[str, Any] = {
            "type": "product",
            "body": {"text": body},
            "action": {
                "catalog_id": catalog_id,
                "product_retailer_id": retailer_id,
            },
        }
        if footer:
            interactive["footer"] = {"text": footer}
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        if context_message_id:
            payload["context"] = {"message_id": context_message_id}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def send_product_list(
        self,
        to: str,
        catalog_id: str,
        sections: list,
        body: str,
        header: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Multi-product section list (Meta "product_list" interactive
        message). sections is [{"title": str, "retailer_ids": [str, ...]}],
        mapped to Meta's sections/product_items shape here so callers pass
        plain retailer ids, not Meta wire format.
        """
        url = f"{self.base_url}/messages"
        interactive: Dict[str, Any] = {
            "type": "product_list",
            "body": {"text": body},
            "action": {
                "catalog_id": catalog_id,
                "sections": [
                    {
                        "title": section["title"],
                        "product_items": [
                            {"product_retailer_id": rid}
                            for rid in section["retailer_ids"]
                        ],
                    }
                    for section in sections
                ],
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def send_product_card_or_text(
        self,
        to: str,
        product,
        fallback_text: str,
        body: Optional[str] = None,
        footer: Optional[str] = None,
        catalog_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Attempts a single-product card for a Product row, falling back to
        the equivalent text send on ANY card failure — no catalog linked
        (settings.CATALOG_ID unset), unmappable row, transport error, or a
        Meta error payload (e.g. catalog not connected to this WABA).
        Callers never handle the fallback themselves; what comes back is
        either the accepted card response or the fallback text response.
        body defaults to fallback_text so the card carries the same line
        the text send would have — one message either way, never both.
        """
        catalog = catalog_id or settings.CATALOG_ID
        retailer_id = str(product.id) if product is not None and product.id is not None else None
        if not catalog or not retailer_id:
            return await self.send_text(to, fallback_text)
        try:
            resp = await self.send_product_message(
                to, catalog, retailer_id, body or fallback_text, footer=footer
            )
        except Exception as e:
            logger.warning(f"Product card send failed; falling back to text: {e}")
            return await self.send_text(to, fallback_text)
        if not isinstance(resp, dict) or resp.get("error") or "messages" not in resp:
            logger.warning(f"Product card rejected ({resp!r}); falling back to text.")
            return await self.send_text(to, fallback_text)
        return resp

    async def upload_media(
        self, file_content: bytes, file_name: str, mime_type: str
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/media"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        files = {"file": (file_name, file_content, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, files=files, data=data)
            return response.json()

    async def send_media(
        self,
        to: str,
        media_id: str,
        media_type: str = "image",
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: {"id": media_id},
        }
        if caption:
            payload[media_type]["caption"] = caption

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()
