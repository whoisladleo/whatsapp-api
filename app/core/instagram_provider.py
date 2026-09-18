import httpx
from typing import Dict, Any
from app.core.config import settings


class InstagramProvider:
    """
    Wraps the Instagram API (Instagram Login variant, graph.instagram.com —
    not graph.facebook.com; there's no Facebook Page in this flow) for
    comment replies and comment-triggered private-reply DMs. Instantiated
    per-account with that account's own Instagram-scoped access token, same
    pattern as WhatsAppProvider.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = f"{settings.INSTAGRAM_GRAPH_BASE_URL}/{settings.WHATSAPP_VERSION}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
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

    async def send_direct_message(
        self, ig_business_id: str, recipient_id: str, message: str
    ) -> Dict[str, Any]:
        # Ongoing DM sends (not tied to a specific comment) address the
        # recipient by their IGSID instead of a comment_id.
        url = f"{self.base_url}/{ig_business_id}/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            return response.json()

    async def get_user_profile(self, igsid: str) -> Dict[str, Any]:
        """
        Looks up a DM sender's profile by IGSID. Meta's messaging webhook
        only ever gives us the sender's id (see
        InstagramWebhookParser.parse_inbound_messages) — unlike the comments
        webhook, which includes from.username directly — so this is the only
        way to get a real display name for a DM-only InstagramCustomer.
        """
        url = f"{self.base_url}/{igsid}"
        params = {"fields": "name,username"}
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=self.headers)
            return response.json()
