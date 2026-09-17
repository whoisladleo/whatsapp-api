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
