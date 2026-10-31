from typing import Any, List, Optional

# Normalized attachment shape stored on Message.attachments /
# InstagramDirectMessage.attachments: {"type": ..., "url": ...?,
# "media_id": ...?, "mime_type": ...?, "filename": ...?}. WhatsApp media has
# no fetchable URL at webhook time (Graph media URLs need a per-view
# resolve and expire in minutes), so WA entries carry media_id only.


def describe_attachments(attachments: Optional[List[Any]]) -> str:
    """
    One-line human description of normalized attachments, for Amara's prompt
    and chat history when a message has no text body (#87). Never raises on
    unexpected shapes — worst case it says "shared content".
    """
    if not attachments:
        return ""
    parts = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("type") or "").lower()
        url = attachment.get("url")
        if kind == "share":
            parts.append(f"shared a post{': ' + url if url else ''}")
        elif kind in ("image", "photo"):
            parts.append(f"sent a photo{': ' + url if url else ''}")
        elif kind == "video":
            parts.append(f"sent a video{': ' + url if url else ''}")
        elif kind == "audio":
            parts.append("sent a voice message")
        elif kind == "document":
            filename = attachment.get("filename")
            parts.append(f"sent a file{': ' + filename if filename else ''}")
        elif kind == "sticker":
            parts.append("sent a sticker")
        elif kind == "story_mention":
            parts.append("mentioned you in their story")
        elif kind == "contacts":
            parts.append("shared a contact")
        elif kind == "location":
            parts.append("shared a location")
        elif kind:
            parts.append(f"sent {kind}")
    if not parts:
        return "shared content"
    return "; ".join(parts)
