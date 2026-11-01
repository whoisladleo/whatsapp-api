import logging
import re

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.ids import coerce_uuid
from app.core.security import normalize_phone
from app.db.models import (
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Order,
    OrderStatus,
    WhatsAppAccount,
)
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Broad EN/SW recall gate for the caller-side enqueue check below: any
# payment-adjacent token in either the inbound or the outbound text earns
# the turn a verification pass. Deliberately over-broad (e.g. bare "TZS",
# "land") — precision is the LLM judge's job; a missed phantom is worse
# than a wasted judge call. Turns whose prose carries no token at all
# ("Okay", "Thanks") skip verification entirely.
PAYMENT_TOKEN_RE = re.compile(
    r"pay|malipo|lip\w*|approv|idhini|ombi|request|sent|tum\w*|prompt|tunzaa"
    r"|transact|muamala|m-?pesa|tigo\s?ypesa|halo\s?ypesa|airtel|mixx|tzs"
    r"|on its way|njiani|\blands?\b|\blanded\b",
    re.IGNORECASE,
)

# One phantom-payment alert per conversation per window — the Oct 31
# incident chat would otherwise have paged the merchant a dozen times
# overnight for the same missing order.
CLAIM_THROTTLE_TTL_SECONDS = 6 * 3600

_JUDGE_SYSTEM = """You are a payment-claim auditor for a sales chatbot. You get ground truth about the customer's orders plus one outbound message. Answer whether the message claims a payment is already underway.

Ground truth arrives as two facts: LIVE_PENDING (a payment request currently awaiting approval) and PRIOR_PAID (an older completed payment).

A claim is YES when the message says a payment request was already sent or is on its way, or presupposes one is in flight ("once you approve it", "after you approve"). It is NO when the message only talks about future or conditional payment ("once you agree, a request will be sent", "you can pay by mobile money"), explains prices, asks for a phone number, reports a payment failure, or confirms an already-completed payment while PRIOR_PAID is yes.

Examples (each shows ground truth, then the message, then the verdict):
LIVE_PENDING=no PRIOR_PAID=no / "You'll get a mobile-money payment request on your phone shortly — just approve it." / YES
LIVE_PENDING=no PRIOR_PAID=no / "It's on its way to your phone — just approve the mobile-money prompt." / YES
LIVE_PENDING=no PRIOR_PAID=no / "Ombi la malipo limetumwa kwenye simu yako — tafadhali liidhinishe." / YES
LIVE_PENDING=no PRIOR_PAID=no / "Once you approve it, I'll be live on your shop right away." / YES
LIVE_PENDING=no PRIOR_PAID=no / "Once you agree, a payment request will be sent to your phone for approval." / NO
LIVE_PENDING=no PRIOR_PAID=no / "The plan is TZS 100,000 per month, no commission on your sales." / NO
LIVE_PENDING=no PRIOR_PAID=no / "Sorry, the payment could not be sent — someone from our team will help." / NO
LIVE_PENDING=no PRIOR_PAID=no / "Tuma namba yako ya simu ili nikutumie ombi la malipo." / NO
LIVE_PENDING=no PRIOR_PAID=yes / "Your payment went through — you're live!" / NO

Reply with exactly one word: YES or NO."""


def _has_payment_tokens(text: str | None) -> bool:
    return bool(text) and PAYMENT_TOKEN_RE.search(text) is not None


def should_verify_payment_claim(
    inbound_body: str | None,
    outbound_body: str | None,
    markers_seen: bool = False,
    order_action: bool = False,
) -> bool:
    """
    Caller-side gate (runs in the reply worker, before enqueue): True when
    this turn is worth a verification pass. A turn that ran the order
    machinery (close/resend/dispatch produced its note) is always clean —
    the note appended to the reply describes something real. A turn that
    saw markers but took no order action is always suspect — this is
    exactly the pre-#100 silent-ignore shape (marker stripped, prose sent,
    no order). Everything else gates on payment-adjacent tokens in
    either direction, since phantom reassurance ("it's on its way")
    usually answers a payment question ("I still haven't got it").
    """
    if order_action:
        return False
    if markers_seen:
        return True
    return _has_payment_tokens(inbound_body) or _has_payment_tokens(outbound_body)


def _throttle_key(channel: str, customer_pk: str) -> str:
    return f"phantom_claim:{channel}:{customer_pk}"


def _throttle_hit(key: str) -> bool:
    import app.core.rate_limit as rate_limit_module

    try:
        return rate_limit_module._redis_client.get(key) is not None
    except Exception:
        # Fail open like the reply lock: a Redis outage must not silence
        # money alerts. Worst case is a duplicate alert per turn.
        return False


def _throttle_set(key: str) -> bool:
    import app.core.rate_limit as rate_limit_module

    try:
        return bool(
            rate_limit_module._redis_client.set(key, "1", nx=True, ex=CLAIM_THROTTLE_TTL_SECONDS)
        )
    except Exception:
        return True


async def _judge_payment_claim(outbound_body: str, prior_paid: bool) -> bool:
    """
    LLM verdict on the one question the exact filters can't answer: does
    this outbound text claim a payment is already underway? Fail-silent
    (False) on any failure — an unjudgeable message must never page a
    merchant, and the judge running at all means no live order exists, so
    "unknown" can only be a false negative, never a false accusation.
    """
    from app.services.llm import _complete_with_retry, llm_service

    if llm_service is None or llm_service.client is None:
        return False
    content = (
        f"LIVE_PENDING=no PRIOR_PAID={'yes' if prior_paid else 'no'}\n"
        f'"{outbound_body}"'
    )
    try:
        # Generous cap for a one-word verdict: reasoning tokens share the
        # budget on both providers, and a length stop here degrades to a
        # false negative (fail-silent), so starving the judge is the one
        # thing this call must not do.
        text = await _complete_with_retry(
            llm_service.provider,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            max_tokens=300,
        )
    except Exception as e:
        logger.warning(f"Payment-claim judge unavailable; skipping: {e}")
        return False
    tokens = (text or "").strip().upper().split()
    if not tokens or tokens[0] not in ("YES", "NO"):
        logger.warning(f"Payment-claim judge returned unparseable verdict: {text!r}")
        return False
    return tokens[0] == "YES"


def _alert_components(identifier: str, message: str) -> list:
    # Same handoff_notification shape as
    # LLMService._send_platform_alert_to_merchant ({{customer}} in BOTH the
    # header and the body — Meta rejects the send otherwise). {{message}}
    # carries a system-written warning here rather than a customer quote.
    return [
        {
            "type": "header",
            "parameters": [
                {"type": "text", "parameter_name": "customer", "text": identifier},
            ],
        },
        {
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": "message", "text": message},
                {"type": "text", "parameter_name": "customer", "text": identifier},
            ],
        },
    ]


async def _alert_merchant(identifier: str, message: str, owner, alert_target: dict) -> int:
    """
    Sends the phantom-payment warning to the merchant's handoff number
    and OPS_ALERT_PHONE via the platform's own credentials (a merchant
    never has an open 24h session with the platform number, so this must
    be the approved template, not free text — see usage_alerts for the
    same pattern). Each send arms tap-to-reply onto the flagged
    conversation. Alert-only: unlike a handoff, this flips no
    conversation state and schedules no reminder nudge. Returns the
    number of sends accepted by Meta.
    """
    from app.core.provider import WhatsAppProvider
    from app.crud import user as crud_user
    from app.services.handoff_flow import remember_alert

    recipients = {
        normalize_phone(p)
        for p in (
            crud_user.resolve_handoff_number(owner) if owner is not None else None,
            settings.OPS_ALERT_PHONE,
        )
        if p
    }
    if not recipients:
        logger.warning("Phantom-payment flag raised but no merchant or OPS number on file; staying silent.")
        return 0
    provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
    components = _alert_components(identifier, message)
    sent = 0
    for to in sorted(recipients):
        try:
            resp = await provider.send_template(
                to,
                settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME,
                language_code=settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_LANGUAGE,
                components=components,
            )
        except Exception as e:
            logger.error(f"Phantom-payment alert to {to} failed: {e}")
            continue
        if "error" in (resp or {}):
            logger.error(f"Phantom-payment alert to {to} rejected: {resp}")
            continue
        sent += 1
        wamid = ((resp.get("messages") or [{}])[0] or {}).get("id")
        if wamid and alert_target is not None:
            remember_alert(wamid, alert_target)
    return sent


@celery_app.task
def verify_payment_claim(
    channel: str,
    account_id: str,
    customer_ref: str,
    inbound_body: str | None,
    outbound_body: str | None,
    order_action: bool = False,
) -> str:
    """
    Async backstop for Amara promising a payment with no order behind it
    (#116 — the Oct 31 chat promised "it's on its way" for 13 hours while
    no order existed). Enqueued after an AI reply sends; exact filters
    first (this turn ran the order machinery / a human already owns the
    chat / a live PENDING order backs the words), then the LLM judge for
    the remainder, then a merchant + OPS template alert on a positive.

    Best-effort by design: a monitor must never break the pipeline it
    watches, so every failure mode below degrades to a log line, never
    an exception.
    """
    try:
        return _verify(channel, account_id, customer_ref, inbound_body, outbound_body, order_action)
    except Exception as e:
        logger.error(f"Payment-claim verification crashed; skipping: {e}")
        return "Error skipped."


def _verify(
    channel: str,
    account_id: str,
    customer_ref: str,
    inbound_body: str | None,
    outbound_body: str | None,
    order_action: bool,
) -> str:
    if order_action:
        return "Skipped: this turn ran the order machinery."

    from app.services.llm import _hold_expired
    from app.tasks.webhook_processing import _run_async

    db = SessionLocal()
    try:
        account_uuid = coerce_uuid(account_id)
        if account_uuid is None:
            return "Account not found."
        if channel == "whatsapp":
            account = db.query(WhatsAppAccount).filter(WhatsAppAccount.id == account_uuid).first()
            customer = (
                db.query(Customer)
                .filter(Customer.phone_number == normalize_phone(customer_ref))
                .first()
            )
            context = (
                db.query(ConversationContext)
                .filter(ConversationContext.customer_id == customer.id)
                .first()
                if customer
                else None
            )
            order_base = (
                db.query(Order).filter(
                    Order.account_id == account_uuid,
                    Order.customer_id == customer.id,
                )
                if customer
                else None
            )
            identifier = customer.phone_number if customer else customer_ref
            customer_pk = str(customer.id) if customer else customer_ref
            alert_target = {
                "channel": "whatsapp",
                "ref": identifier,
                "name": identifier,
                "account_id": str(account_uuid),
                "customer_id": None,
            }
        elif channel == "instagram":
            customer_uuid = coerce_uuid(customer_ref)
            account = db.query(InstagramAccount).filter(InstagramAccount.id == account_uuid).first()
            customer = db.get(InstagramCustomer, customer_uuid) if customer_uuid else None
            context = (
                db.query(InstagramConversationContext)
                .filter(InstagramConversationContext.customer_id == customer.id)
                .first()
                if customer
                else None
            )
            order_base = (
                db.query(Order).filter(
                    Order.instagram_account_id == account_uuid,
                    Order.instagram_customer_id == customer.id,
                )
                if customer
                else None
            )
            identifier = f"@{customer.username or customer.ig_scoped_id}" if customer else customer_ref
            customer_pk = str(customer.id) if customer else customer_ref
            alert_target = {
                "channel": "instagram",
                "ref": customer.username or customer.ig_scoped_id if customer else customer_ref,
                "name": identifier,
                "account_id": None,
                "customer_id": str(customer.id) if customer else None,
            }
        else:
            return "Unknown channel."
        if not account or not customer or not context:
            return "Account, customer, or conversation not found."
        if context.state == ConversationState.HUMAN_HANDOFF:
            return "Skipped: conversation is in human handoff."

        pending = (
            order_base.filter(Order.status == OrderStatus.PENDING)
            .order_by(Order.created_at.desc())
            .first()
        )
        if pending is not None and not _hold_expired(pending):
            return "Skipped: a live pending order backs this reply."
        prior_paid = (
            order_base.filter(Order.status == OrderStatus.PAID)
            .order_by(Order.created_at.desc())
            .first()
        )

        throttle_key = _throttle_key(channel, customer_pk)
        if _throttle_hit(throttle_key):
            return "Throttled: already alerted for this conversation."

        flagged = _run_async(_judge_payment_claim(outbound_body or "", prior_paid is not None))
        if not flagged:
            return "Clear: no payment claim."
        if not _throttle_set(throttle_key):
            return "Throttled: alert already sent."

        preview = (outbound_body or "").strip().replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:197] + "..."
        message = (
            f"Phantom-payment flag: Amara told {identifier} a payment is on its way, "
            f'but no order exists behind it. Flagged reply: "{preview}". Check the chat.'
        )
        owner = account.owner
        sent = _run_async(_alert_merchant(identifier, message, owner, alert_target))
        return f"Flagged: merchant alerted ({sent} send(s))."
    finally:
        db.close()
