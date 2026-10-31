from app.core.attachments import describe_attachments


def test_describe_empty_and_garbage():
    assert describe_attachments(None) == ""
    assert describe_attachments([]) == ""
    assert describe_attachments(["not-a-dict"]) == "shared content"
    assert describe_attachments([{}]) == "shared content"


def test_describe_share_with_and_without_url():
    assert (
        describe_attachments([{"type": "share", "url": "https://ig.example/p/1"}])
        == "shared a post: https://ig.example/p/1"
    )
    assert describe_attachments([{"type": "share"}]) == "shared a post"


def test_describe_media_kinds():
    assert (
        describe_attachments([{"type": "image", "url": "https://cdn/x.jpg"}])
        == "sent a photo: https://cdn/x.jpg"
    )
    assert describe_attachments([{"type": "image", "media_id": "m1"}]) == "sent a photo"
    assert describe_attachments([{"type": "video"}]) == "sent a video"
    assert describe_attachments([{"type": "audio"}]) == "sent a voice message"
    assert describe_attachments([{"type": "sticker"}]) == "sent a sticker"
    assert (
        describe_attachments([{"type": "document", "filename": "price.pdf"}])
        == "sent a file: price.pdf"
    )
    assert describe_attachments([{"type": "contacts"}]) == "shared a contact"
    assert describe_attachments([{"type": "story_mention"}]) == "mentioned you in their story"


def test_describe_joins_multiple_attachments():
    assert (
        describe_attachments([{"type": "image"}, {"type": "sticker"}])
        == "sent a photo; sent a sticker"
    )
