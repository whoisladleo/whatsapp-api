import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import (
    AmaraEventKind,
    ConversationContext,
    ConversationState,
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    User,
    WhatsAppAccount,
)
from app.services import usage
from app.services.llm.helpers import _truncate_for_alert

logger = logging.getLogger("app.services.llm")


class NotifyMixin:
    async def _trigger_human_handoff(
        self,
        db: Session,
        account: WhatsAppAccount,
        context: ConversationContext,
        from_phone: str,
        customer_message: str,
        is_platform_account: bool,
    ) -> None:
        """
        Marks the conversation HUMAN_HANDOFF and alerts the merchant.
        Shared by both handoff triggers: the model's own [HUMAN_HANDOFF]
        marker (tone-based judgment) and the hard failed_cycles counter
        (Rule 3 — see FAILED_CYCLES_HANDOFF_THRESHOLD). Does not touch this
        turn's customer-facing response_text; callers decide what to send.
        customer_message is what the *customer* said, not Amara's own
        reply — without it the merchant has nothing to actually respond to
        when they use /reply. Threaded through into the handoff_notification
        template as its {{message}} variable (see _send_platform_alert_to_merchant)
        — the template was updated in WhatsApp Manager specifically to add
        this slot, rather than dropping the customer's message from the
        alert once alerts moved off free text.
        """
        context.state = ConversationState.HUMAN_HANDOFF
        context.handoff_at = datetime.utcnow()
        db.commit()
        usage.record_event(db, account.user_id, AmaraEventKind.HANDOFF, channel="whatsapp", account_id=account.id)

        # Platform handoffs alert the owner too (#140): a prospect needing
        # a human on Taskflow's own sales number is the highest-value
        # notification in the system, not noise to suppress. The reminder
        # nudge below re-notifies the same owner, so it stays correct here.
        if not account.owner:
            logger.warning(
                "Skipping handoff alert: WhatsAppAccount %s has no owner.", account.id
            )
            return

        await self._send_platform_alert_to_merchant(
            db,
            account.owner,
            from_phone,
            customer_message,
            alert_target={
                "channel": "whatsapp",
                "ref": from_phone,
                "name": from_phone,
                "account_id": str(account.id),
                "customer_id": None,
            },
        )

        from app.tasks.handoff_reminders import (
            send_handoff_reminder,
            HANDOFF_REMINDER_DELAY_SECONDS,
        )
        try:
            send_handoff_reminder.apply_async(
                args=[str(context.id)], countdown=HANDOFF_REMINDER_DELAY_SECONDS
            )
        except Exception as sched_err:
            logger.error(f"Failed to schedule handoff reminder: {sched_err}")

    async def _hand_off_instagram(
        self, db: Session, account: InstagramAccount, customer: InstagramCustomer,
        context: InstagramConversationContext, customer_message: str,
    ) -> None:
        context.state = ConversationState.HUMAN_HANDOFF
        context.handoff_at = datetime.utcnow()
        db.commit()
        usage.record_event(
            db, account.user_id, AmaraEventKind.HANDOFF, channel="instagram_dm", instagram_account_id=account.id
        )
        # No platform carve-out: prospect handoffs on Taskflow's own
        # account alert the owner like any other (#140).
        await self._notify_merchant_of_instagram_handoff(db, account, customer, customer_message)

    async def _notify_merchant_of_instagram_handoff(
        self, db: Session, ig_account: InstagramAccount, customer: InstagramCustomer, customer_message: str
    ) -> None:
        """
        Instagram has no way to message the account owner directly — only
        customers who've messaged the connected account (see CLAUDE.md's
        Instagram section on notify_merchant's identical limitation for
        subscription-renewal alerts) — so a DM handoff is surfaced via
        WhatsApp instead, the same cross-channel pattern
        app/services/product_subscription.py's notify_merchant already
        uses. Sent via the platform's own default WhatsApp credentials
        (see _send_platform_alert_to_merchant) — the merchant's personal
        number is how they talk to Amara/Taskflow itself, independent of
        whether they've connected their own business WhatsAppAccount.
        Unlike _trigger_human_handoff's WhatsApp-native equivalent, this
        doesn't also schedule a 15-minute reminder nudge — that task
        resolves the account to alert from the customer's last *WhatsApp*
        message, which has no equivalent for an Instagram-originated
        handoff. customer_message is what the *customer* said, not
        Amara's own reply — without it the merchant has nothing to
        actually respond to when they use /reply.
        """
        merchant = ig_account.owner
        if not merchant:
            logger.warning(
                "Skipping IG handoff alert: InstagramAccount %s has no owner.", ig_account.id
            )
            return

        # /reply's Instagram path matches on username OR ig_scoped_id (see
        # _relay_to_instagram_customer), so this identifier is always
        # something the merchant can actually paste back in, even before
        # the display-name backfill has ever run for a brand-new customer.
        reply_identifier = customer.username or customer.ig_scoped_id
        await self._send_platform_alert_to_merchant(
            db,
            merchant,
            f"@{reply_identifier}",
            customer_message,
            alert_target={
                "channel": "instagram",
                "ref": reply_identifier,
                "name": f"@{reply_identifier}",
                "account_id": None,
                "customer_id": str(customer.id),
            },
        )

    async def _send_platform_alert_to_merchant(
        self,
        db: Session,
        merchant: User,
        reply_identifier: str,
        customer_message: str,
        alert_target: dict | None = None,
    ) -> None:
        """
        Sends a handoff alert to the merchant's own personal WhatsApp
        number via the approved handoff_notification template
        (WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME), using the platform's own
        default credentials — the same ones send_otp_message uses — rather
        than requiring the merchant's own business WhatsAppAccount.

        A template send is required here, not free text: WhatsApp only
        allows free-form messages to a recipient with an open 24h session
        (i.e. someone who's messaged this number back recently), and a
        merchant essentially never has — they only ever *receive* from the
        platform's number. This was a real production incident: the old
        free-text send got a 200 OK from Meta's API on every attempt, yet
        never actually reached the merchant's phone, silently dropped as
        an out-of-window re-engagement message. Templates are exempt from
        that restriction.

        handoff_notification has a header ("A customer {{customer}} needs
        your help right now.") and a body ("They said: \"{{message}}\" /
        Reply directly using: /reply {{customer}} \"your message\"") —
        {{customer}} appears in BOTH, so it must be supplied in both the
        header and body components' parameters, not just once (get this
        wrong and Meta rejects the whole send, the same way the OTP
        button sub_type mismatch did). reply_identifier is what the
        merchant should paste into /reply — a bare phone number for a
        WhatsApp-native handoff, or "@username"/"@ig_scoped_id" for an
        Instagram one (see call sites). customer_message is truncated to
        HANDOFF_ALERT_MESSAGE_MAX_CHARS for the template's {{message}}
        variable.

        Best-effort if the merchant has no phone on file at all
        (explicit handoff number, then OTP username, then the
        self-reported phone_number — see crud_user.resolve_handoff_number):
        the send is skipped, but loudly — a mute skip here once cost an
        owner every handoff notification with nothing in the logs (#140).
        """
        from app.crud import user as crud_user

        merchant_phone = crud_user.resolve_handoff_number(merchant)
        if not merchant_phone:
            logger.warning(
                "Skipping handoff alert: merchant %s has no dialable number "
                "(handoff_phone_number/username/phone_number all unset or undialable).",
                merchant.id,
            )
            return

        from app.core.config import settings
        from app.core.provider import WhatsAppProvider

        components = [
            {
                "type": "header",
                "parameters": [
                    {"type": "text", "parameter_name": "customer", "text": reply_identifier},
                ],
            },
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": "message", "text": _truncate_for_alert(customer_message)},
                    {"type": "text", "parameter_name": "customer", "text": reply_identifier},
                ],
            },
        ]

        provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
        try:
            alert_resp = await provider.send_template(
                merchant_phone,
                settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME,
                language_code=settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_LANGUAGE,
                components=components,
            )
        except Exception as alert_err:
            logger.error(f"Failed to send platform alert to merchant: {alert_err}")
            alert_resp = None

        # Meta error bodies are NOT exceptions — send_template returns
        # {"error": ...} as a normal value. Without this check a rejected
        # send was recorded as "sent" with nothing in the logs (#148).
        if alert_resp is not None and not usage.whatsapp_send_succeeded(alert_resp):
            logger.error("Handoff alert to merchant %s rejected by Meta: %s", merchant.id, alert_resp)
            alert_resp = None

        # Maps the sent alert to its customer so a button tap's context id
        # resolves to the exact conversation (template button payloads are
        # static — see app/services/handoff_flow.py).
        if alert_target is not None and alert_resp and "messages" in alert_resp:
            from app.services.handoff_flow import remember_alert

            wamid = (alert_resp["messages"][0] or {}).get("id")
            if wamid:
                remember_alert(wamid, alert_target)

        # Only log this as a Message if the platform's own number is
        # actually connected as a real WhatsAppAccount (see CLAUDE.md) —
        # until then there's no account row to attach it to, and the send
        # itself still succeeds/fails independent of that being logged.
        platform_account = (
            db.query(WhatsAppAccount)
            .filter(WhatsAppAccount.phone_number_id == settings.PHONE_NUMBER_ID)
            .first()
        )
        if not platform_account:
            return

        from app.crud import message as crud_message
        crud_message.create_message(
            db,
            {
                "to_phone": merchant_phone,
                "type": "template",
                "body": f"{settings.WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME}: {reply_identifier} — {_truncate_for_alert(customer_message)}",
                "direction": "outbound",
                "status": "sent" if alert_resp else "failed",
                "raw": alert_resp or {},
            },
            platform_account.id,
        )
