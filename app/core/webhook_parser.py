from typing import Any, Dict, List, Optional


class WebhookParser:
    @staticmethod
    def extract_attachments(msg_type: Optional[str], message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Normalizes a WhatsApp media/sticker/contacts message into the shared
        attachment shape (app/core/attachments.py). Also reused to hydrate
        pre-#87 rows (which kept only raw_data) on read.
        """
        if msg_type in ("image", "document", "audio", "video"):
            media = message.get(msg_type, {}) or {}
            entry: Dict[str, Any] = {"type": msg_type}
            if media.get("id"):
                entry["media_id"] = media["id"]
            if media.get("mime_type"):
                entry["mime_type"] = media["mime_type"]
            if media.get("filename"):
                entry["filename"] = media["filename"]
            return [entry]
        if msg_type == "sticker":
            media = message.get("sticker", {}) or {}
            entry = {"type": "sticker"}
            if media.get("id"):
                entry["media_id"] = media["id"]
            return [entry]
        if msg_type == "contacts":
            return [{"type": "contacts"}]
        return []

    @staticmethod
    def parse_inbound(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parses Meta Webhook JSON into a list of simplified message objects.
        """
        parsed_messages = []

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")

                # Handle Messages
                for message in value.get("messages", []):
                    msg_type = message.get("type")
                    from_phone = message.get("from")
                    msg_id = message.get("id")

                    body = ""
                    location = None
                    attachments = WebhookParser.extract_attachments(msg_type, message)
                    if msg_type == "text":
                        body = message.get("text", {}).get("body", "")
                    elif msg_type == "button":
                        body = message.get("button", {}).get("text", "")
                    elif msg_type == "interactive":
                        body = (
                            message.get("interactive", {})
                            .get("button_reply", {})
                            .get("title", "")
                        )
                    elif msg_type in ["image", "document", "audio", "video"]:
                        # The media id used to land in body (noise for Amara
                        # and the dashboard); the caption is the readable
                        # part, the media itself lives in attachments (#87).
                        body = message.get(msg_type, {}).get("caption", "")
                    elif msg_type == "location":
                        loc = message.get("location", {})
                        latitude = loc.get("latitude")
                        longitude = loc.get("longitude")
                        name = loc.get("name")
                        address = loc.get("address")
                        location = {
                            "latitude": latitude,
                            "longitude": longitude,
                            "name": name,
                            "address": address,
                        }
                        label = name or address
                        body = label if label else f"📍 {latitude},{longitude}"

                    item = {
                        "phone_number_id": phone_number_id,
                        "whatsapp_message_id": msg_id,
                        "from_phone": from_phone,
                        "type": msg_type,
                        "body": body,
                        "direction": "inbound",
                        "raw": message,
                    }
                    if location is not None:
                        item["location"] = location
                    if attachments:
                        item["attachments"] = attachments
                    parsed_messages.append(item)

                # Handle Status Updates (delivered, read, etc)
                for status in value.get("statuses", []):
                    parsed_messages.append(
                        {
                            "phone_number_id": phone_number_id,
                            "whatsapp_message_id": status.get("id"),
                            "status": status.get("status"),
                            "direction": "outbound",
                            "event": "status_update",
                            "raw": status,
                        }
                    )

        return parsed_messages
