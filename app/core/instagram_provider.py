import logging

import httpx
from typing import Any, Dict, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)


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
        self, ig_business_id: str, recipient_id: str, message: str, tag: str | None = None
    ) -> Dict[str, Any]:
        # Ongoing DM sends (not tied to a specific comment) address the
        # recipient by their IGSID instead of a comment_id.
        url = f"{self.base_url}/{ig_business_id}/messages"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": message},
        }
        if tag:
            # Meta only allows an untagged reply within 24h of the
            # customer's last message. A genuine human agent reply (manual
            # merchant sends, not Amara's own automated DM flow) can arrive
            # well after that, so these pass tag="HUMAN_AGENT" to stay valid
            # up to 7 days out (see #13) — applied unconditionally rather
            # than only once the window has actually lapsed, since it's
            # still valid usage either way and avoids a last-message-
            # timestamp lookup at send time for no real benefit.
            payload["messaging_type"] = "MESSAGE_TAG"
            payload["tag"] = tag
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=self.headers)
            result = response.json()

        if tag and "reviewed and approved" in result.get("error", {}).get("message", ""):
            # The HUMAN_AGENT tag itself is gated by Meta until the feature
            # is approved via App Review (see #14) — this rejects the send
            # regardless of whether the conversation is actually within or
            # past the 24h window. Until approved, fall back to an untagged
            # send: this fixes the (common) within-window case immediately,
            # and the past-24h case keeps failing exactly as it did before
            # #13, with no code change needed once Meta approves it (see #26).
            untagged_payload = {k: v for k, v in payload.items() if k not in ("messaging_type", "tag")}
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=untagged_payload, headers=self.headers)
                return response.json()

        return result

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

    async def get_media(self, media_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetches a media node (the parent post a comment was left on) by id.
        Returns a normalized dict with caption, media_url, media_type,
        permalink, and children — or None when the media is deleted/private
        or anything else goes wrong. Never raises: this runs inside the
        webhook path, where a failed lookup must fall back to a
        media-unaware reply, not a dropped comment.
        """
        url = f"{self.base_url}/{media_id}"
        params = {
            "fields": "caption,media_url,media_type,permalink,children{media_url,media_type}"
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, headers=self.headers)
                payload = response.json()
        except Exception as e:
            logger.warning("get_media(%s) request failed: %s", media_id, e)
            return None

        if not isinstance(payload, dict) or "error" in payload:
            # Deleted/private media (or a bad id) comes back as an error
            # payload, e.g. "Unsupported get request" — not exceptional.
            logger.warning("get_media(%s) returned an error payload: %s", media_id, payload)
            return None

        media_type = payload.get("media_type")
        media_url = payload.get("media_url")
        children = (payload.get("children") or {}).get("data", []) or []

        if media_type == "CAROUSEL_ALBUM":
            # Carousel selection rule: a carousel's own node carries no
            # media_url, so use the first IMAGE child — images are the
            # useful input for B2's vision step, and first keeps it
            # deterministic. Falls back to the first child of any type
            # when the carousel has no image (all-video), or None when it
            # has no children at all.
            picked = next(
                (c for c in children if c.get("media_type") == "IMAGE"),
                None,
            ) or (children[0] if children else None)
            media_url = (picked or {}).get("media_url")
            media_type = (picked or {}).get("media_type")

        return {
            "id": media_id,
            "caption": payload.get("caption"),
            "media_url": media_url,
            "media_type": media_type,
            "permalink": payload.get("permalink"),
            "children": children,
        }
