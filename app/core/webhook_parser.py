from typing import Any, Dict, List, Optional


class WebhookParser:
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
                        body = message.get(msg_type, {}).get("id", "")
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
