import httpx
from typing import Dict, Any
from app.core.config import settings


class InstagramProvider:
    """
    Wraps the Instagram Graph API (Facebook Login variant, graph.facebook.com)
    for comment replies and comment-triggered private-reply DMs. Instantiated
    per-account with that account's own Page access token, same pattern as
    WhatsAppProvider.
    """

    def __init__(self, page_access_token: str):
        self.access_token = page_access_token
        self.base_url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}"
        self.headers = {
            "Authorization": f"Bearer {page_access_token}",
            "Content-Type": "application/json",
        }

    async def reply_to_comment(self, comment_id: str, message: str) -> Dict[str, Any]:
        url = f"{self.base_url}/{comment_id}/replies"
        payload = {"message": message}
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def send_private_reply(
        self, ig_business_id: str, comment_id: str, message: str
    ) -> Dict[str, Any]:
        # Meta allows exactly one of these per comment, within 7 days of it
        # being made — see InstagramComment.private_reply_sent.
        url = f"{self.base_url}/{ig_business_id}/messages"
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": message},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()
