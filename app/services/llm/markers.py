import logging
import re
from typing import Optional

logger = logging.getLogger("app.services.llm")


_AGREEMENT_RE = re.compile(
    r"\[AGREEMENT_REACHED:\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.,]+)\s+payment=(?P<payment>full|installment)"
    r"(?:\s+phone=(?P<phone>\S+))?\s*\]",
    re.IGNORECASE,
)

_QUOTE_RE = re.compile(
    r"\[QUOTE:\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.,]+)\s*\]",
    re.IGNORECASE,
)

_RESEND_RE = re.compile(r"\[RESEND_PAYMENT(?P<params>[^\]]*)\]", re.IGNORECASE)
_RESEND_PARAMS_RE = re.compile(
    r":\s*product=(?P<product>.+?)\s+price=(?P<price>[\d.,]+)\s+payment=(?P<payment>full|installment)"
    r"(?:\s+phone=(?P<phone>\S+))?\s*$",
    re.IGNORECASE,
)


def _parse_and_strip_resend(text: str):
    """
    Extracts the [RESEND_PAYMENT] marker the model emits when the buyer
    says the payment request never arrived (see the system prompts).
    The bracket always strips — even with garbage inside, the marker
    never reaches the customer. Returns a params dict — empty for the
    bare marker (enough when a live order exists to re-fire against) —
    or None when no marker is present. Unparseable params degrade to
    the bare form with a warning.
    """
    match = _RESEND_RE.search(text)
    if not match:
        return text, None
    cleaned = _RESEND_RE.sub("", text).strip()
    raw_params = (match.group("params") or "").strip()
    if not raw_params:
        return cleaned, {}
    params = _RESEND_PARAMS_RE.match(raw_params)
    if not params:
        logger.warning("RESEND_PAYMENT with unparseable params; treating as bare: %r", match.group(0))
        return cleaned, {}
    try:
        price = float(params.group("price").replace(",", ""))
    except ValueError:
        logger.warning("RESEND_PAYMENT with unparseable price; treating as bare: %r", match.group(0))
        return cleaned, {}
    product_name = params.group("product").strip()
    if not product_name:
        return cleaned, {}
    return cleaned, {
        "product_name": product_name,
        "price": price,
        "payment_type": params.group("payment").lower(),
        "phone": params.group("phone"),
    }


_COMMENT_REPLY_RE = re.compile(
    r"PUBLIC:\s*(?P<public>.+?)\s*\n+\s*PRIVATE:\s*(?P<private>.+)",
    re.IGNORECASE | re.DOTALL,
)

# Only "en"/"sw" are real options — matches Customer.language's only
# consumers (WHATSAPP_OTP_TEMPLATE_LANGUAGE / WHATSAPP_REMINDER_TEMPLATE_LANGUAGE),
# and every customer-facing template on this account is registered under
# exactly these two codes (see CLAUDE.md).
_LANGUAGE_RE = re.compile(r"\[LANGUAGE:\s*(?P<language>en|sw)\s*\]", re.IGNORECASE)


def _parse_and_strip_language(text: str):
    """
    Extracts the one-shot [LANGUAGE: en|sw] marker generate_response asks
    the model to emit only while Customer.language is still unset (see the
    system prompt below) — the model already infers/matches the customer's
    language every turn per its existing VOICE instructions, this just
    captures that as structured data instead of only shaping prose. Always
    strips the marker from what the customer sees, whether or not it
    parses.
    """
    match = _LANGUAGE_RE.search(text)
    if not match:
        return text, None
    cleaned = _LANGUAGE_RE.sub("", text).strip()
    return cleaned, match.group("language").lower()


def _parse_comment_reply(text: str):
    """
    Parses the PUBLIC:/PRIVATE: two-line format generate_comment_reply asks
    the model for. Returns None on any malformed/empty output rather than
    guessing — the caller falls back to not sending anything sooner than
    risk sending a broken or empty message publicly.
    """
    match = _COMMENT_REPLY_RE.search(text)
    if not match:
        return None
    public_reply = match.group("public").strip()
    private_reply = match.group("private").strip()
    if not public_reply or not private_reply:
        return None
    return public_reply, private_reply


def _parse_and_strip_quote(text: str):
    """
    Extracts the [QUOTE: product=... price=...] marker the model is
    instructed to emit whenever it states a specific price during
    negotiation but hasn't closed the deal yet — used to schedule a
    "still interested?" follow-up if the customer goes quiet (see
    app/tasks/quote_nudges.py). Same parse-and-always-strip pattern as
    _parse_and_strip_agreement.
    """
    match = _QUOTE_RE.search(text)
    if not match:
        return text, None

    cleaned = _QUOTE_RE.sub("", text).strip()

    try:
        price = float(match.group("price").replace(",", ""))
    except ValueError:
        return cleaned, None

    product_name = match.group("product").strip()
    if not product_name:
        return cleaned, None

    return cleaned, {"product_name": product_name, "price": price}


def _parse_and_strip_agreement(text: str):
    """
    Extracts the [AGREEMENT_REACHED: product=... price=... payment=...]
    marker the model is instructed to emit once a deal closes (regular-
    merchant branch only — see the system prompt below), the same way
    [HUMAN_HANDOFF] is parsed. Always strips the marker from what the
    customer sees, whether or not it parses; a malformed marker just means
    no order gets created.

    The optional `phone=` group only ever appears in Instagram DM output
    (see generate_dm_response's prompt) — the WhatsApp flow already knows
    the customer's phone number from the conversation itself, so its prompt
    never asks the model to include one, and `match.group("phone")` is
    simply None there.
    """
    match = _AGREEMENT_RE.search(text)
    if not match:
        idx = text.upper().find("AGREEMENT_REACHED")
        if idx != -1:
            # The model tried to close but the marker didn't parse — the
            # buyer was told it's a deal with nothing behind it (#113).
            # Loud on purpose: this class used to fail silently.
            logger.warning(
                "Unparseable AGREEMENT_REACHED marker; no order created: %r",
                text[max(0, idx - 60):idx + 120],
            )
        return text, None

    cleaned = _AGREEMENT_RE.sub("", text).strip()

    try:
        price = float(match.group("price").replace(",", ""))
    except ValueError:
        return cleaned, None

    product_name = match.group("product").strip()
    if not product_name:
        return cleaned, None

    return cleaned, {
        "product_name": product_name,
        "price": price,
        "payment_type": match.group("payment").lower(),
        "phone": match.group("phone"),
    }


def _mark_turn_outcome(turn_outcome: Optional[dict], key: str) -> None:
    # Records what this turn did for the post-send payment-claim check
    # (#116): "markers_seen" when an agreement/resend marker parsed (even
    # one that ends up ignored), "order_action" when the order machinery
    # ran and produced its note. Callers that don't care pass nothing.
    if turn_outcome is not None:
        turn_outcome[key] = True
