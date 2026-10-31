from typing import Any, Dict, List


class InstagramWebhookParser:
    @staticmethod
    def parse_inbound(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parses Meta's Instagram webhook JSON into a list of simplified
        comment objects. `entry[].id` is the receiving IG business account,
        not the commenter — that's under `changes[].value.from`.
        """
        parsed_comments = []

        for entry in data.get("entry", []):
            ig_business_id = entry.get("id")

            for change in entry.get("changes", []):
                if change.get("field") != "comments":
                    continue

                value = change.get("value", {})
                from_ = value.get("from", {})
                media = value.get("media", {})

                # Meta sends a webhook event for every comment on the
                # media, including replies posted BY the connected account
                # itself (from.id == the receiving account's own id) — our
                # own public reply to a customer's comment is exactly such
                # an event. Without this, Amara would treat her own reply
                # as a new inbound comment and reply to it again, forever.
                if from_.get("id") == ig_business_id:
                    continue

                parsed_comments.append(
                    {
                        "ig_business_id": ig_business_id,
                        "comment_id": value.get("id"),
                        "media_id": media.get("id"),
                        "from_username": from_.get("username"),
                        "from_ig_scoped_id": from_.get("id"),
                        "body": value.get("text", ""),
                        "raw": value,
                    }
                )

        return parsed_comments

    @staticmethod
    def parse_attachments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Normalizes Meta's IG `message.attachments` (forwards, shares,
        photos, stickers — none of which carry `text`) into the shared
        attachment shape (app/core/attachments.py). Unknown entries are
        kept with their raw type so they still render as *something*.
        """
        normalized = []
        for attachment in message.get("attachments", []) or []:
            payload = attachment.get("payload", {}) or {}
            kind = attachment.get("type", "file")
            if kind == "image" and payload.get("sticker_id") and not payload.get("url"):
                kind = "sticker"
            entry: Dict[str, Any] = {"type": kind}
            if payload.get("url"):
                entry["url"] = payload["url"]
            normalized.append(entry)
        return normalized

    @staticmethod
    def parse_inbound_messages(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parses Meta's Instagram webhook JSON for DM events — a different
        shape from comments: `entry[].messaging`, not `entry[].changes`.
        Skips echoes of our own sent messages (`message.is_echo`) and
        non-message events (read receipts etc., which have no `message`
        key) — both would otherwise loop the same way unfiltered comment
        self-replies did.
        """
        parsed_messages = []

        for entry in data.get("entry", []):
            ig_business_id = entry.get("id")

            for event in entry.get("messaging", []):
                message = event.get("message")
                if not message or message.get("is_echo"):
                    continue

                sender_id = event.get("sender", {}).get("id")
                if not sender_id or sender_id == ig_business_id:
                    continue

                item = {
                    "ig_business_id": ig_business_id,
                    "sender_id": sender_id,
                    "ig_message_id": message.get("mid"),
                    "body": message.get("text", ""),
                }
                attachments = InstagramWebhookParser.parse_attachments(message)
                if attachments:
                    item["attachments"] = attachments
                parsed_messages.append(item)

        return parsed_messages

    @staticmethod
    def parse_outbound_echoes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Surfaces Meta's echoes of shop-side DM sends (`message.is_echo`)
        — deliberately separate from parse_inbound_messages, which keeps
        dropping every echo. An echo's sender is the connected account
        itself, so the customer is resolved from `recipient.id` instead.
        handle_webhook tells the cases apart by mid: an echo whose id is
        already saved is Amara's own send (ignore); a new mid is something
        a person typed in the Instagram inbox — a hand-sent opener when
        Amara hasn't replied in that conversation yet, otherwise a
        mid-chat takeover that puts the conversation in HUMAN_HANDOFF.
        """
        parsed_echoes = []

        for entry in data.get("entry", []):
            ig_business_id = entry.get("id")

            for event in entry.get("messaging", []):
                message = event.get("message")
                if not message or not message.get("is_echo"):
                    continue

                customer_id = event.get("recipient", {}).get("id")
                if not customer_id or customer_id == ig_business_id:
                    continue

                item = {
                    "ig_business_id": ig_business_id,
                    "sender_id": customer_id,
                    "ig_message_id": message.get("mid"),
                    "body": message.get("text", ""),
                    "is_echo": True,
                }
                attachments = InstagramWebhookParser.parse_attachments(message)
                if attachments:
                    item["attachments"] = attachments
                parsed_echoes.append(item)

        return parsed_echoes
