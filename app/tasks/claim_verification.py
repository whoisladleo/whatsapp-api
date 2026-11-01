import logging
import re
from datetime import datetime

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.ids import coerce_uuid
from app.core.security import normalize_phone
from app.db.models import (
    AmaraEventKind,
    ConversationContext,
    ConversationState,
    Customer,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    InstagramDirectMessage,
    Message,
    Order,
    OrderStatus,
    Product,
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

_JUDGE_SYSTEM = """You are a payment-claim auditor for a sales chatbot. You get ground truth about the customer's orders plus the last few messages of the conversation (oldest first, ending with Amara's latest reply). Answer five questions about that latest reply.

Ground truth arrives as two facts: LIVE_PENDING (a payment request currently awaiting approval) and PRIOR_PAID (an older completed payment). Here LIVE_PENDING is always no.

Reply with exactly these five lines and nothing else:
CLAIM: YES if Amara's latest reply says a payment request was already sent or is on its way, or presupposes one is in flight ("once you approve it", "after you approve"). NO if it only talks about future or conditional payment ("once you agree, a request will be sent", "you can pay by mobile money"), explains prices, asks for a phone number, reports a payment failure, or confirms an already-completed payment while PRIOR_PAID is yes.
PRODUCT: the product or plan name this payment is for, exactly as Amara named it anywhere in these messages, or NONE if no product is named.
PRICE: the total TZS amount the buyer would pay, digits only with no commas or decimals, or NONE if no amount is stated. For a monthly plan, give the monthly amount.
CONSENT: YES if the buyer agreed to buy ("lets do this", "yes", "I want it") or asked for / chased the payment ("I'm waiting for the payment request", "I still did not get it"). NO if there is no agreement or request, or the buyer refused, cancelled, or asked to stop.
PHONE: a phone number the buyer gave in their messages, digits only, or NONE.

Examples (each shows the message slice, then the verdict):
buyer: Lets do this
amara: Perfect — you're in! Confirming: Taskflow Entry Plan at TZS 100,000/month, full payment, billed monthly. You'll get a mobile-money payment request on your phone shortly — just approve it.
CLAIM: YES
PRODUCT: Taskflow Entry Plan
PRICE: 100000
CONSENT: YES
PHONE: NONE

buyer: Lets do this
amara: Perfect — you're in! Confirming: Taskflow Entry Plan at TZS 100,000/month. You'll get a payment request shortly.
buyer: Hello i still did not get it
amara: You're welcome! Just shout if that payment request still hasn't shown up.
CLAIM: YES
PRODUCT: Taskflow Entry Plan
PRICE: 100000
CONSENT: YES
PHONE: NONE

buyer: Tuma kwa 0712345678
amara: Sawa! Ombi la malipo la TZS 50,000 la Widget limetumwa kwenye simu yako — tafadhali liidhinishe.
CLAIM: YES
PRODUCT: Widget
PRICE: 50000
CONSENT: YES
PHONE: 0712345678

buyer: Actually stop, I changed my mind
amara: It's on its way to your phone — just approve the mobile-money prompt.
CLAIM: YES
PRODUCT: NONE
PRICE: NONE
CONSENT: NO
PHONE: NONE

buyer: Bei gani?
amara: The Taskflow Entry Plan is TZS 100,000 per month, no commission. Once you agree, a payment request will be sent to your phone.
CLAIM: NO
PRODUCT: Taskflow Entry Plan
PRICE: 100000
CONSENT: NO
PHONE: NONE"""


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


def _parse_verdict(text: str) -> dict | None:
    """
    Parses the judge's five-line verdict into a dict, or None on garbage.
    Lenient about extra lines (reasoning models narrate), strict about
    the five values: CLAIM/CONSENT must be YES/NO, PRICE digits or NONE.
    """
    fields: dict = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().upper()
        if key in ("CLAIM", "PRODUCT", "PRICE", "CONSENT", "PHONE"):
            fields[key] = value.strip()
    if set(fields) != {"CLAIM", "PRODUCT", "PRICE", "CONSENT", "PHONE"}:
        logger.warning(f"Claim judge verdict missing fields: {sorted(fields)}")
        return None
    claim = fields["CLAIM"].rstrip(".").upper()
    consent = fields["CONSENT"].rstrip(".").upper()
    if claim not in ("YES", "NO") or consent not in ("YES", "NO"):
        logger.warning(f"Claim judge verdict has bad flags: {claim}/{consent}")
        return None
    price = None
    if fields["PRICE"].upper() != "NONE":
        digits = fields["PRICE"].replace(",", "").replace(" ", "")
        if not digits.isdigit():
            logger.warning(f"Claim judge verdict has bad price: {fields['PRICE']!r}")
            return None
        price = int(digits)
    return {
        "claim": claim == "YES",
        "product": None if fields["PRODUCT"].upper() == "NONE" else fields["PRODUCT"],
        "price": price,
        "consent": consent == "YES",
        "phone": None if fields["PHONE"].upper() == "NONE" else fields["PHONE"],
    }


async def _judge_payment_claim(history: list, prior_paid: bool) -> dict | None:
    """
    LLM verdict over a conversation slice (oldest first, ending with
    Amara's latest reply): is a payment claimed, and — for recovery —
    what terms did the buyer agree to? Returns None on any failure
    (judge down, unparseable): the caller hands off and alerts on None,
    because the Oct 31 incident proved a silent checker costs more than
    a cautious handoff — the merchant resolves it with one tap.
    Uses the raw completion path, never delegation, so the judge can
    never route, act, or spend.

    Builds a fresh client per call instead of reusing the shared
    llm_service: the shared SDK client holds a connection pool, and this
    task runs each call inside its own asyncio.run, so every other call
    reuses a pooled connection bound to an already-closed loop and dies
    with "Event loop is closed" (measured 5/10 alternating failures on
    the muse provider). A per-call client costs one connection setup and
    is immune. The reply path has the same latent flaw on rapid
    back-to-back turns — tracked separately, out of #120's scope.
    """
    from app.services.llm import _complete_with_retry, _default_llm_service

    service = _default_llm_service()
    if service is None or service.client is None:
        return None
    transcript = "\n".join(
        f"{'buyer' if direction == 'inbound' else 'amara'}: {body}"
        for direction, body in history
    )
    content = (
        f"LIVE_PENDING=no PRIOR_PAID={'yes' if prior_paid else 'no'}\n{transcript}"
    )
    try:
        # Generous cap for a five-line verdict: reasoning tokens share the
        # budget on both providers, and a length stop here degrades to
        # handoff-without-recovery, so starving the judge is the one thing
        # this call must not do.
        text = await _complete_with_retry(
            service.provider,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            max_tokens=300,
        )
    except Exception as e:
        logger.warning(f"Payment-claim judge unavailable; handing off: {e}")
        return None
    if not (text or "").strip():
        logger.warning("Payment-claim judge returned empty response")
        return None
    return _parse_verdict(text)


def _recent_history(db, channel: str, account_uuid, customer, outbound_fallback: str | None) -> list:
    """
    Last few turns of this conversation, oldest first, for the judge.
    A phantom reassurance ("it's on its way", chasing a payment the
    buyer asked about two turns ago) is only recognizable — and only
    recoverable — with the surrounding turns in view.
    """
    rows: list = []
    if channel == "whatsapp":
        # Both sides normalized: stored rows and the customer row disagree
        # on trunk-prefix format often enough ("0712…" vs "255712…") that
        # an exact SQL match would drop turns.
        want = normalize_phone(customer.phone_number)
        candidates = (
            db.query(Message)
            .filter(
                Message.account_id == account_uuid,
                Message.type == "text",
                Message.body.isnot(None),
            )
            .order_by(Message.timestamp.desc())
            .limit(60)
            .all()
        )
        for row in candidates:
            other = row.from_phone if row.direction == "inbound" else row.to_phone
            if normalize_phone(other) == want:
                rows.append(row)
            if len(rows) == 6:
                break
        rows.reverse()
    else:
        rows = list(
            reversed(
                db.query(InstagramDirectMessage)
                .filter(
                    InstagramDirectMessage.customer_id == customer.id,
                    InstagramDirectMessage.body.isnot(None),
                )
                .order_by(InstagramDirectMessage.created_at.desc())
                .limit(6)
                .all()
            )
        )
    history = [(row.direction, (row.body or "")[:500]) for row in rows if (row.body or "").strip()]
    if not history and (outbound_fallback or "").strip():
        history = [("outbound", outbound_fallback.strip()[:500])]
    return history


def _handoff_conversation(db, channel: str, account, context) -> None:
    """
    Pauses Amara on a flagged chat: the same state flip + HANDOFF ledger
    entry the model's own [HUMAN_HANDOFF] uses (see
    LLMService._trigger_human_handoff), minus its merchant alert — the
    caller sends one combined alert covering flag and handoff together.
    """
    from app.services import usage as usage_service

    context.state = ConversationState.HUMAN_HANDOFF
    context.handoff_at = datetime.utcnow()
    db.commit()
    if channel == "whatsapp":
        usage_service.record_event(
            db, account.user_id, AmaraEventKind.HANDOFF, channel="whatsapp", account_id=account.id
        )
        from app.tasks.handoff_reminders import (
            HANDOFF_REMINDER_DELAY_SECONDS,
            send_handoff_reminder,
        )

        try:
            send_handoff_reminder.apply_async(
                args=[str(context.id)], countdown=HANDOFF_REMINDER_DELAY_SECONDS
            )
        except Exception as sched_err:
            logger.error(f"Failed to schedule handoff reminder: {sched_err}")
    else:
        usage_service.record_event(
            db,
            account.user_id,
            AmaraEventKind.HANDOFF,
            channel="instagram_dm",
            instagram_account_id=account.id,
        )


def _resolve_terms(db, account, context, verdict: dict):
    """
    Turns the judge's extracted product + price into a real (product,
    price) pair, or (None, None, why-not) for the merchant alert. Every
    check biases toward refusing: an auto-recovery that charges for the
    wrong thing is worse than a handoff the merchant resolves by hand.
    """
    name = verdict["product"]
    if not name:
        return None, None, "no product named in the chat"
    matches = (
        db.query(Product)
        .filter(
            Product.merchant_id == account.user_id,
            Product.is_draft.is_(False),
            Product.name.ilike(name),
        )
        .all()
    )
    if not matches:
        return None, None, f"no product matching '{name}'"
    if len(matches) > 1:
        return None, None, f"multiple products matching '{name}'"
    product = matches[0]
    intent = getattr(context, "current_product_intent", None)
    if intent and intent.lower() != product.name.lower():
        return None, None, f"chat intent ({intent}) disagrees"
    price = verdict["price"]
    if price is None:
        price = int(product.max_price)
    elif not (product.min_price <= price <= product.max_price):
        return None, None, f"TZS {price:,} outside '{product.name}' band"
    return product, price, None


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
    conversation. This is the handoff alert, not a side note: the caller
    already flipped the conversation to HUMAN_HANDOFF before calling.
    Returns the number of sends accepted by Meta.
    """
    from app.core.provider import WhatsAppProvider
    from app.crud import user as crud_user
    from app.services.handoff_flow import remember_alert

    if len(message) > 350:
        message = message[:347] + "..."

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
    no order existed; #120 made the flag actionable). Enqueued after an
    AI reply sends; exact filters first (this turn ran the order
    machinery / a human already owns the chat / a live PENDING order
    backs the words), then the LLM judge over the last few turns. On a
    positive (or an unjudgeable turn) the chat is handed to the merchant,
    the promised order is rebuilt from the agreed terms when they check
    out, and one combined merchant + OPS template alert covers both.

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

        history = _recent_history(db, channel, account_uuid, customer, outbound_body)
        verdict = _run_async(_judge_payment_claim(history, prior_paid is not None))
        if verdict is not None and not verdict["claim"]:
            return "Clear: no payment claim."
        if not _throttle_set(throttle_key):
            return "Throttled: alert already sent."

        # Flagged (or unjudgeable): pause Amara first, then try to rebuild
        # the promised order from the agreed terms. The merchant gets one
        # combined alert either way.
        _handoff_conversation(db, channel, account, context)
        recovered = None
        if verdict is None:
            short, detail = "judge_unavailable", "the payment checker was down, so I paused Amara to be safe"
        elif not verdict["consent"]:
            short, detail = "no_consent", "couldn't confirm the buyer agreed — no auto-recovery"
        else:
            product, price, terms_why = _resolve_terms(db, account, context, verdict)
            if terms_why is not None:
                short, detail = "no_terms", f"no safe terms to recover ({terms_why})"
            else:
                phone = (
                    customer.phone_number
                    if channel == "whatsapp"
                    else verdict["phone"]
                )
                if not (phone and normalize_phone(phone)):
                    short, detail = "no_phone", "no buyer phone number on file — no auto-recovery"
                else:
                    from app.services.payment_resend import close_deal_from_terms

                    parties = (account, customer, context, phone, channel == "instagram")
                    _, reason = _run_async(
                        close_deal_from_terms(db, parties, product, price)
                    )
                    if reason is None:
                        recovered = (product, price)
                        short, detail = "recovered", ""
                    else:
                        short = f"recover_{reason}"
                        detail = {
                            "out_of_stock": f"'{product.name}' is out of stock — no order created",
                            "deal_invalid": "the deal is no longer valid (product or price changed)",
                            "tunzaa_error": "could not reach Tunzaa — no order created",
                            "already_owned": "this plan is already paid for — no new prompt was sent",
                            "unavailable": "closer unavailable — no order created",
                        }.get(reason, "recovery failed — no order created")

        if recovered is not None:
            product, price = recovered
            message = (
                f"Phantom-payment flag on {identifier}: Amara promised a payment with no order, "
                f"so I paused Amara and recreated it — {product.name} at TZS {price:,}, prompt "
                f"re-sent. Chat handed to you; tap to reply."
            )
        else:
            preview = (outbound_body or "").strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:197] + "..."
            headline = (
                "Amara promised a payment with no order behind it"
                if verdict is not None
                else "a payment-adjacent reply went out with no order behind it"
            )
            # First character only: str.capitalize would also lowercase
            # product names quoted inside the detail.
            detail = detail[:1].upper() + detail[1:]
            message = (
                f"Phantom-payment flag on {identifier}: {headline}. {detail}. "
                f'Chat handed to you; tap to reply. Flagged reply: "{preview}".'
            )
        owner = account.owner
        sent = _run_async(_alert_merchant(identifier, message, owner, alert_target))
        if recovered is not None:
            return f"Flagged: recovered {recovered[0].name} order, merchant alerted ({sent} send(s))."
        return f"Flagged: handed off without recovery ({short}), merchant alerted ({sent} send(s))."
    finally:
        db.close()
