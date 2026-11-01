from datetime import timedelta


# How long a reservation (and the matching order_reminders schedule) holds
# stock before auto_cancel_order releases it — see app/tasks/order_reminders.py.
RESERVATION_WINDOW = timedelta(hours=48)


# Minimum gap between two Tunzaa prompts for the same order — an
# impatient buyer mashing "didn't get it" must not machine-gun payment
# prompts (#113). Keyed off Order.updated_at, which every dispatch
# touches.
RESEND_MIN_INTERVAL = timedelta(minutes=5)


# Rule 3 (README/CLAUDE.md): "after 3 failed price agreement attempts...
# switch context to HUMAN_HANDOFF". ConversationContext.failed_cycles counts
# consecutive [QUOTE] rounds on the same product (see _track_quote) — hitting
# this many force-escalates regardless of what the model itself judges.
FAILED_CYCLES_HANDOFF_THRESHOLD = 3


# Handoff alerts quote the customer's own message so the merchant has
# something to actually respond to (see _trigger_human_handoff /
# _notify_merchant_of_instagram_handoff) — bounded so an unusually long
# customer message can't blow up the alert or exceed the handoff_notification
# template's {{message}} variable's practical length.
HANDOFF_ALERT_MESSAGE_MAX_CHARS = 200


# The model only ever sees a floor 10% above the real min_price, so whatever
# it leaks or concedes in free text is still above what the merchant
# approved. _close_deal/_close_instagram_deal still enforce the real
# min_price as the hard guarantee (#37).
NEGOTIATION_FLOOR_BUFFER = 1.10
