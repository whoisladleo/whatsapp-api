"""Amara's sales brain, split by job (#111).

Public surface stays exactly what it was when this was one file: every
name imported anywhere from app.services.llm is re-exported here, and
internal call sites that tests patch through this namespace
(_try_claim_stock, _reclose_guard_note, match_post_to_product) call back
through it (see the llm_facade imports), so behavior patches keep landing.
"""
import httpx  # noqa: F401  (tests patch app.services.llm.httpx.AsyncClient)
import os  # noqa: F401  (tests patch app.services.llm.os.path.exists)

from app.core.config import settings
from app.services.llm.close import CloseMixin
from app.services.llm.constants import RESEND_MIN_INTERVAL, RESERVATION_WINDOW
from app.services.llm.helpers import (
    DealRejected,
    _hold_expired,
    _is_out_of_stock,
    _lang_text,
    _negotiation_floor,
    _reclose_guard_note,
    _truncate_for_alert,
    _try_claim_stock,
)
from app.services.llm.markers import (
    _mark_turn_outcome,
    _parse_and_strip_agreement,
    _parse_and_strip_language,
    _parse_and_strip_quote,
    _parse_and_strip_resend,
    _parse_comment_reply,
)
from app.services.llm.notify import NotifyMixin
from app.services.llm.prompts import _holding_reply, _outage_reply
from app.services.llm.providers import (
    ClaudeProvider,
    LLMLengthCutoff,
    LLMProvider,
    LLMUnavailable,
    MuseProvider,
    _complete_with_retry,
    _is_transient_llm_error,
)
from app.services.llm.replies import ReplyMixin
from app.services.llm.replies_dm import DMReplyMixin
from app.services.llm.replies_wa import WAReplyMixin
from app.services.llm.service import LLMService, _default_llm_service, llm_service
from app.services.vision_match import match_post_to_product

__all__ = [
    "CloseMixin",
    "ClaudeProvider",
    "DMReplyMixin",
    "DealRejected",
    "LLMLengthCutoff",
    "LLMProvider",
    "LLMService",
    "LLMUnavailable",
    "MuseProvider",
    "NotifyMixin",
    "ReplyMixin",
    "WAReplyMixin",
    "RESEND_MIN_INTERVAL",
    "RESERVATION_WINDOW",
    "_complete_with_retry",
    "_default_llm_service",
    "_hold_expired",
    "_holding_reply",
    "_is_out_of_stock",
    "_is_transient_llm_error",
    "_lang_text",
    "_mark_turn_outcome",
    "_negotiation_floor",
    "_outage_reply",
    "_parse_and_strip_agreement",
    "_parse_and_strip_language",
    "_parse_and_strip_quote",
    "_parse_and_strip_resend",
    "_parse_comment_reply",
    "_reclose_guard_note",
    "_truncate_for_alert",
    "_try_claim_stock",
    "httpx",
    "llm_service",
    "match_post_to_product",
    "os",
    "settings",
]
