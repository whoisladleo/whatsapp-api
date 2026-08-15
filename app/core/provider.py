import httpx
from typing import Optional, Dict, Any, Union
from app.core.config import settings


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
        self, to: str, body: str, preview_url: bool = False
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

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
