from typing import Optional


_HOLDING_REPLIES = {
    "sw": "Asante! Naomba nithibitishe hili na duka kwanza — mtu wa timu yetu atakujibu hapa hivi punde.",
    "en": "Thanks! Let me confirm this with the shop first — someone from our team will reply here shortly.",
}


def _holding_reply(language: Optional[str]) -> str:
    """
    Replaces Amara's reply when a deal she just agreed to can't be closed —
    otherwise the buyer has been told a price and nothing follows (#37).
    Both languages when the customer's language isn't known (Instagram
    never classifies it).
    """
    if language in _HOLDING_REPLIES:
        return _HOLDING_REPLIES[language]
    return f"{_HOLDING_REPLIES['sw']}\n\n{_HOLDING_REPLIES['en']}"


_PAYMENT_REQUEST_LINES = {
    "sw": "Ombi la malipo la TZS {price} limetumwa kwenye simu yako — tafadhali liidhinishe ili kukamilisha oda yako.",
    "en": "A payment request for TZS {price} has been sent to your phone — please approve it to complete your order.",
}

_PAYMENT_RESEND_LINES = {
    "sw": "Nimetuma tena ombi la malipo kwenye simu yako — liangalie na uliidhinishe.",
    "en": "I've re-sent the payment request to your phone — check for the latest prompt and approve it.",
}

_PAYMENT_RESEND_THROTTLED_LINES = {
    "sw": "Ombi limetumwa muda mfupi uliopita — tafadhali angalia simu yako, huenda tayari limefika.",
    "en": "A request went out just moments ago — please check your phone, it may already be there.",
}

_PAYMENT_DISPATCH_FAILED_LINES = {
    "sw": "Kuna tatizo kuanzisha malipo — tafadhali jaribu tena baada ya muda mfupi.",
    "en": "Something went wrong setting up your payment — please try again in a moment.",
}

_INSTALLMENT_LINES = {
    "sw": "Mpango wako wa malipo ya awamu umeundwa! Tutawasiliana nawe na maelezo ya malipo.",
    "en": "Your installment plan has been created! We'll be in touch with payment details.",
}

# Base delivery ask, no joiner — the FULL and installment branches
# prepend their own ("While you're at it…" / "Please also…").
_LOCATION_NOTES = {
    "instagram": {
        "sw": "jibu hapa na anwani yako ya kupelekewa ili tujue pa kukuletea",
        "en": "reply here with your delivery address so we know where to deliver it",
    },
    "whatsapp": {
        "sw": "shiriki eneo lako la kupelekewa kwa kutumia kipengele cha kushiriki eneo cha WhatsApp (ikoni ya 📎) ili tujue pa kukuletea",
        "en": "share your delivery location using WhatsApp's location-sharing feature (the 📎 attach icon) so we know where to deliver it",
    },
}

_DELIVERY_JOINERS = {
    "full": {"sw": "Wakati huo huo, tafadhali", "en": "While you're at it, please"},
    "installment": {"sw": "Tafadhali pia", "en": "Please also"},
}

_ALREADY_SENT_LINES = {
    "sw": "Ombi la malipo la TZS {price} lishafika kwenye simu yako — tafadhali liidhinishe ili kukamilisha oda yako.",
    "en": "A payment request for TZS {price} is already on its way to your phone — please approve it to complete your order.",
}

_ALREADY_ACTIVE_LINES = {
    "sw": "Usajili wako wa {product} bado unaendelea — hakuna malipo mapya yanayohitajika.",
    "en": "Your {product} subscription is already active — no new payment needed.",
}


_OUTAGE_REPLIES = {
    "sw": "Samahani, nina tatizo la kiufundi kwa sasa — mtu wa timu yetu atakujibu hapa hivi punde.",
    "en": "Sorry, I'm having a technical problem right now — someone from our team will reply here shortly.",
}


def _outage_reply(language: Optional[str]) -> str:
    """Sent once Claude retries are exhausted; the chat is handed to the merchant at the same time (#38)."""
    if language in _OUTAGE_REPLIES:
        return _OUTAGE_REPLIES[language]
    return f"{_OUTAGE_REPLIES['sw']}\n\n{_OUTAGE_REPLIES['en']}"
