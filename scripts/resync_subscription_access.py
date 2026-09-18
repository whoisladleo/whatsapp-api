"""
One-off backfill for the subscription-access gating feature (#4):
sync_ai_access_for_merchant only ever runs when something *triggers* it (a
ProductSubscription status change, or a WhatsApp/Instagram account create/
update/reconnect) — it never runs proactively just because
PLATFORM_SUBSCRIPTION_PRODUCT_SLUG got set in .env. Every merchant who
existed before that deploy keeps whatever ai_enabled they already had until
something else touches their account. This script forces the check for
every User right now, so "no exemptions, applies retroactively" actually
takes effect immediately instead of only for merchants who happen to touch
their settings afterward.

Run once after deploying #4 (and again any time PLATFORM_SUBSCRIPTION_PRODUCT_SLUG
changes to a different product):

    python scripts/resync_subscription_access.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.db.models import User
from app.db.session import SessionLocal
from app.services.subscription_access import is_merchant_allowed_ai, sync_ai_access_for_merchant


def resync_all() -> None:
    if not settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG:
        print(
            "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG is not set — gating is a no-op, "
            "nothing to resync. Set it in .env first."
        )
        return

    db = SessionLocal()
    try:
        users = db.query(User).all()
        print(f"Resyncing ai_enabled for {len(users)} user(s) against "
              f"'{settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG}'...")

        denied = 0
        allowed = 0
        for user in users:
            before = is_merchant_allowed_ai(db, user)
            sync_ai_access_for_merchant(db, user)
            if before:
                allowed += 1
            else:
                denied += 1

        print(f"Done. {allowed} merchant(s) allowed, {denied} merchant(s) denied "
              f"(ai_enabled forced off on their WhatsApp/Instagram accounts).")
    finally:
        db.close()


if __name__ == "__main__":
    resync_all()
