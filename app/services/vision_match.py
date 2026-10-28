"""Vision product-match for Instagram comment replies (B2, Muse-only).

When a comment arrives on a photo post, this matches the post image against
the merchant's catalog so the comment reply can name the pictured product.
Only the Muse provider uses it — the Muse chat-completions endpoint accepts
image_url content parts, and the Claude path stays exactly as it was.

Transport is always server-side download + base64 data URL: Meta's API
rejects remote image URLs outright (400 "URL is not fetchable by this
service", verified live), so there is no URL-passing mode.

Everything here fails soft: any missing/unusable media, download problem,
or model error returns None, and generate_comment_reply falls back to its
current media-unaware behavior. Nothing in this module ever raises.
"""

import base64
import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# The reply itself is one product name, but the budget still needs real
# headroom: at 100 tokens the model spends everything on reasoning and
# returns content=None with finish_reason=length (verified live, 3/3
# failures) — the same failure mode MuseProvider documents for text
# replies. 500 matches the comment/DM budget and returns 3/3.
VISION_MAX_TOKENS = 500

# The download runs inside the comment webhook path; bound it so a slow
# CDN can't stall the reply.
VISION_DOWNLOAD_TIMEOUT_SECONDS = 5.0
VISION_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _vision_catalog(products: List) -> str:
    lines = ["Merchant catalog:"]
    for p in products:
        lines.append(f"- {p.name}: Price TZS {p.max_price:,.0f}")
        if p.description:
            lines.append(f"  {p.description}")
    return "\n".join(lines)


def _parse_match(reply_text: Optional[str], products: List):
    """
    Accepts only an exact (case-insensitive) catalog name or NO_MATCH.
    Anything else — prose, a hedged answer, an invented name — is no
    match: the reply may only ever name products from the catalog.
    """
    if not reply_text:
        return None
    cleaned = reply_text.strip().strip("\"'").strip()
    if not cleaned or cleaned.upper() == "NO_MATCH":
        return None
    for p in products:
        if p.name.strip().lower() == cleaned.lower():
            return p
    logger.info("Vision match %r is not a catalog product — ignoring", cleaned)
    return None


async def _download_image_as_data_url(url: str) -> Optional[str]:
    """Fetches an image server-side into a base64 data URL. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=VISION_DOWNLOAD_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.warning(
                        "Vision image download got HTTP %s for %s",
                        response.status_code, url,
                    )
                    return None
                content_type = response.headers.get("content-type", "") or ""
                if content_type and not content_type.startswith("image/"):
                    logger.warning(
                        "Vision image download got non-image content-type %r for %s",
                        content_type, url,
                    )
                    return None
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks += chunk
                    if len(chunks) > VISION_MAX_IMAGE_BYTES:
                        logger.warning(
                            "Vision image exceeds %d bytes, skipping: %s",
                            VISION_MAX_IMAGE_BYTES, url,
                        )
                        return None
    except Exception as e:
        logger.warning("Vision image download failed for %s: %s", url, e)
        return None
    mime = content_type.split(";")[0].strip() or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(bytes(chunks)).decode()


async def match_post_to_product(
    *, client, model: str, post_media: Optional[dict], products: List
):
    """
    Returns the single catalog Product the post image shows, or None.

    client is an OpenAI-compatible async client (MuseProvider.client),
    post_media is InstagramProvider.get_media's normalized dict, products
    are the merchant's Product rows. Never raises — callers treat None as
    "reply without vision context".
    """
    try:
        if not post_media or not products:
            return None
        media_url = post_media.get("media_url")
        if not media_url:
            return None
        # Only still images are usable vision input. get_media normalizes
        # a carousel to its picked child, so a surviving VIDEO type (or a
        # video-only carousel) genuinely has no image to match.
        if post_media.get("media_type") not in (None, "IMAGE"):
            return None

        data_url = await _download_image_as_data_url(media_url)
        if not data_url:
            return None

        response = await client.chat.completions.create(
            model=model,
            max_tokens=VISION_MAX_TOKENS,
            reasoning_effort="minimal",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You match a photo from a shop's Instagram post "
                                "against the merchant's product catalog.\n\n"
                                f"{_vision_catalog(products)}\n\n"
                                "Look at the post image and reply with ONLY the "
                                "exact name of the single best-matching catalog "
                                "product above, or NO_MATCH if the photo does not "
                                "clearly show any of them. No other text."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        choice = response.choices[0]
        return _parse_match(choice.message.content, products)
    except Exception as e:
        logger.warning("Vision product-match failed: %s", e)
        return None
