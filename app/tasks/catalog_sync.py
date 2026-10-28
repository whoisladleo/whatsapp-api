import logging
import time

from app.core.catalog_client import (
    CatalogError,
    CatalogMappingError,
    MetaCatalogClient,
    product_to_catalog_item,
)
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.ids import coerce_uuid
from app.db.models import Product, User
from app.db.session import SessionLocal
from app.tasks.webhook_processing import _run_async

logger = logging.getLogger(__name__)

OP_UPSERT = "upsert"
OP_DELETE = "delete"

# Bounded status polling for an items_batch POST: per-item errors only
# surface on check_batch_request_status, not on the POST itself, so a sync
# that never polls can't log loudly about a rejected item. Small and
# best-effort — an unfinished batch after these attempts just logs.
STATUS_POLL_ATTEMPTS = 6
STATUS_POLL_DELAY_SECONDS = 5
# Anything not in this set counts as terminal (finished, failed, or an
# unknown shape we must not spin on forever). "started" proven live (E1):
# check_batch_request_status reports it while the batch is still running.
_PENDING_STATUSES = {"pending", "queued", "in_progress", "running", "started"}


def _catalog_enabled() -> bool:
    return bool(settings.CATALOG_ID and settings.CATALOG_ACCESS_TOKEN)


def dispatch_catalog_sync(product_id, op: str = OP_UPSERT) -> bool:
    """
    Fire-and-forget catalog sync after a local product write. Returns True
    when the task was queued. Never raises: credentials unset is a silent
    no-op (zero behavior change for existing deploys), and a dead broker
    logs loudly instead of breaking the write that just succeeded.
    """
    if not _catalog_enabled():
        return False
    try:
        sync_catalog_item.apply_async(args=[str(product_id), op])
    except Exception as e:
        logger.error(
            "Catalog sync dispatch failed for product %s (op=%s): %s",
            product_id, op, e,
        )
        return False
    return True


def _poll_batch_handles(client: MetaCatalogClient, response, item_id: str) -> None:
    """
    Best-effort status poll for the handles an items_batch POST returned.
    Logs per-item errors loudly; never raises.
    """
    handles = (response or {}).get("handles") if isinstance(response, dict) else None
    if not handles:
        logger.warning(
            "Catalog sync for %s got no batch handles to poll: %r",
            item_id, response,
        )
        return
    for handle in handles:
        for attempt in range(STATUS_POLL_ATTEMPTS):
            try:
                payload = _run_async(client.get_batch_status(handle))
            except CatalogError as e:
                logger.error(
                    "Catalog status poll failed for %s (handle %s): %s",
                    item_id, handle, e,
                )
                break
            entries = payload.get("data") if isinstance(payload, dict) else None
            entry = None
            if isinstance(entries, list) and entries:
                entry = next(
                    (e for e in entries if isinstance(e, dict) and e.get("handle") == handle),
                    entries[0] if isinstance(entries[0], dict) else None,
                )
            if entry is None:
                logger.warning(
                    "Catalog status poll for %s returned an unexpected shape: %r",
                    item_id, payload,
                )
                break
            errors_total = entry.get("errors_total_count") or 0
            errors = entry.get("errors") or []
            if errors_total or errors:
                logger.error(
                    "Catalog batch for %s reported %s error(s): %r",
                    item_id, errors_total or len(errors), errors,
                )
            status = str(entry.get("status") or "").lower()
            if status not in _PENDING_STATUSES:
                break
            if attempt + 1 < STATUS_POLL_ATTEMPTS:
                time.sleep(STATUS_POLL_DELAY_SECONDS)
        else:
            # Only reachable when every attempt saw a pending status (every
            # other path breaks out of the loop).
            logger.warning(
                "Catalog batch for %s (handle %s) still pending after %d polls.",
                item_id, handle, STATUS_POLL_ATTEMPTS,
            )


@celery_app.task
def sync_catalog_item(product_id: str, op: str = OP_UPSERT):
    """
    Upserts or deletes one catalog item by id (= str(Product.id), the same
    value D3 messaging sends as product_retailer_id).

    Best-effort always: Meta outages, mapping failures (missing image,
    unusable merchant phone, unresolvable brand name), and a product deleted
    between dispatch and execution all log loudly and return a reason string
    — never raise, so the local write that queued this task can never break,
    including under Celery eager execution in tests.
    """
    if not _catalog_enabled():
        # Silent no-op: catalog sync is off unless both settings are set.
        return "Catalog sync disabled."
    if op not in (OP_UPSERT, OP_DELETE):
        logger.error("Unknown catalog sync op %r for product %s.", op, product_id)
        return f"Unknown op: {op}"
    client = MetaCatalogClient(settings.CATALOG_ID, settings.CATALOG_ACCESS_TOKEN)
    try:
        if op == OP_DELETE:
            # The row is already gone locally; nothing to re-read.
            response = _run_async(client.delete_items([str(product_id)]))
            _poll_batch_handles(client, response, str(product_id))
            return "Deleted."
        db = SessionLocal()
        try:
            product_uuid = coerce_uuid(product_id)
            product = db.get(Product, product_uuid) if product_uuid else None
            if not product:
                logger.warning(
                    "Skipping catalog upsert for product %s: no such row "
                    "(deleted between dispatch and execution).", product_id,
                )
                return "Product not found; skipped."
            merchant = db.get(User, product.merchant_id) if product.merchant_id else None
            merchant_phone = None
            merchant_name = None
            if merchant:
                merchant_phone = merchant.username or merchant.phone_number
                # Brand fallback chain — shop/account names over phone digits:
                # User.business_name is the explicit brand field; then the
                # merchant's first WhatsApp account display name (the shop
                # name customers see, always set when an account exists);
                # then the personal User.name; and only last the phone
                # digits (ugly but valid — better than skipping the sync).
                # merchant_phone already folds username/phone_number, so one
                # trailing fallback covers both.
                account_name = next(
                    (a.name for a in (merchant.accounts or []) if a and a.name),
                    None,
                )
                merchant_name = (
                    merchant.business_name
                    or account_name
                    or merchant.name
                    or merchant_phone
                )
            try:
                item = product_to_catalog_item(product, merchant_phone, merchant_name)
            except CatalogMappingError as e:
                logger.warning(
                    "Skipping catalog upsert for product %s: %s", product_id, e,
                )
                return f"Skipped: {e}"
            response = _run_async(client.upsert_items([item]))
            _poll_batch_handles(client, response, str(product_id))
            return "Upserted."
        finally:
            db.close()
    except CatalogError as e:
        logger.error("Catalog sync failed for product %s: %s", product_id, e)
        return f"Failed: {e}"
    except Exception:
        # Truly unexpected (e.g. a DB error inside the task) — still best
        # effort, still never breaking the local write.
        logger.exception("Catalog sync crashed for product %s", product_id)
        return "Failed: unexpected error."
