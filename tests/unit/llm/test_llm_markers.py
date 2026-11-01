from app.db.models import (
    Product,
)
from app.services.llm import (
    _holding_reply,
    _negotiation_floor,
    _parse_and_strip_agreement,
    _parse_and_strip_language,
    _parse_and_strip_quote,
    _truncate_for_alert,
)


def test_parse_agreement_extracts_multi_word_product_and_strips_marker():
    text, agreement = _parse_and_strip_agreement(
        "Great, let's do it! [AGREEMENT_REACHED: product=Running Shoes price=45000 payment=full]"
    )

    assert text == "Great, let's do it!"
    assert agreement == {
        "product_name": "Running Shoes",
        "price": 45000.0,
        "payment_type": "full",
        "phone": None,
    }


def test_parse_agreement_case_insensitive():
    text, agreement = _parse_and_strip_agreement(
        "[agreement_reached: PRODUCT=Shoes PRICE=45000 PAYMENT=INSTALLMENT]"
    )

    assert agreement["payment_type"] == "installment"
    assert agreement["product_name"] == "Shoes"


def test_parse_agreement_extracts_optional_phone_for_instagram_dm_flow():
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=Shoes price=45000 payment=full phone=255711130000]"
    )

    assert text == "Deal!"
    assert agreement["phone"] == "255711130000"


def test_parse_agreement_no_marker_present():
    text, agreement = _parse_and_strip_agreement("Just a normal reply.")

    assert text == "Just a normal reply."
    assert agreement is None


def test_parse_agreement_missing_field_not_matched():
    original = "Deal! [AGREEMENT_REACHED: product=Shoes price=45000]"

    text, agreement = _parse_and_strip_agreement(original)

    assert agreement is None
    assert text == original  # regex didn't match at all, nothing stripped


def test_parse_agreement_blank_product_name_returns_none():
    # Two spaces between "product=" and "price=" lets the non-greedy .+?
    # capture a single whitespace character, which strips to empty.
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=  price=45000 payment=full]"
    )

    assert agreement is None
    assert "AGREEMENT_REACHED" not in text


def test_parse_agreement_unparseable_price_returns_none():
    text, agreement = _parse_and_strip_agreement(
        "Deal! [AGREEMENT_REACHED: product=Shoes price=45.65.23 payment=full]"
    )

    assert agreement is None
    assert "AGREEMENT_REACHED" not in text  # still stripped even though unusable


def test_truncate_for_alert_leaves_short_text_unchanged():
    assert _truncate_for_alert("Do you have this in size 42?") == "Do you have this in size 42?"


def test_truncate_for_alert_truncates_long_text_with_ellipsis():
    long_text = "a" * 250
    result = _truncate_for_alert(long_text, max_chars=200)
    assert len(result) == 201  # 200 chars + the ellipsis character
    assert result.endswith("…")
    assert result.startswith("a" * 200)


def test_parse_and_strip_quote_extracts_and_strips_marker():
    text, quote = _parse_and_strip_quote(
        "I can do TZS 45,000. [QUOTE: product=Shoes price=45000]"
    )
    assert text == "I can do TZS 45,000."
    assert quote == {"product_name": "Shoes", "price": 45000.0}


def test_parse_and_strip_quote_no_marker_returns_none():
    text, quote = _parse_and_strip_quote("Just chatting, no price yet.")
    assert text == "Just chatting, no price yet."
    assert quote is None


def test_parse_and_strip_language_extracts_and_strips_marker():
    text, language = _parse_and_strip_language("Karibu! [LANGUAGE: sw]")
    assert text == "Karibu!"
    assert language == "sw"


def test_parse_and_strip_language_case_insensitive():
    text, language = _parse_and_strip_language("Hello! [language: EN]")
    assert text == "Hello!"
    assert language == "en"


def test_parse_and_strip_language_no_marker_returns_none():
    text, language = _parse_and_strip_language("Just a normal reply.")
    assert text == "Just a normal reply."
    assert language is None


def test_negotiation_floor_is_ten_percent_above_min_capped_at_max():
    assert _negotiation_floor(Product(min_price=40000, max_price=50000)) == 44000
    assert _negotiation_floor(Product(min_price=100000, max_price=100000)) == 100000
    assert _negotiation_floor(Product(min_price=48000, max_price=50000)) == 50000


def test_holding_reply_matches_known_language_else_both():
    assert _holding_reply("sw").startswith("Asante")
    assert _holding_reply("en").startswith("Thanks")
    assert "Asante" in _holding_reply(None) and "Thanks" in _holding_reply(None)
