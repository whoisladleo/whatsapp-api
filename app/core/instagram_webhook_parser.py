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
