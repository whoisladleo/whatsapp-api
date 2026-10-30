"""
Meta Commerce catalog client (D1: client + mapping only).

D1 is deliberately client + mapping + docs — NO product-lifecycle hooks.
D2 wires create/update/delete hooks; E1 runs the staging rehearsal.

Transport follows the existing httpx provider pattern (app/core/provider.py,
app/core/instagram_provider.py): per-call AsyncClient, Bearer auth, Graph API
version/base from settings. Every failure — transport error, non-JSON body,
HTTP error status, or a Meta "error" payload — surfaces as CatalogError.
Nothing is ever swallowed or returned as a falsy success.

One-time Business Manager / catalog / token setup lives in CATALOG_SETUP.md
(next to this package's repo root), not here.

PRODUCT_ITEM Supported Fields (corrected in the E1 staging-fix round after
Meta rejected our first shape live with "Can not find required field id",
then verified field-by-field against the official items_batch Supported
Fields spec for PRODUCT_ITEM):
  Sent on CREATE (upsert): id, title, description, price, availability,
  condition, image, link, brand.
  - "id" is the unique content ID — NOT "retailer_id" on this wire shape.
    The value is unchanged (str(Product.id)), so D3 messaging (which sends
    the same value as product_retailer_id) stays consistent.
  - "title" (max 100 chars, truncated defensively here) — NOT "name".
  - "image" is an array of {"url": ...} objects (single entry, no tags) —
    NOT an "image_url" string.
  - "brand" is Required and was missing entirely: the merchant shop name,
    resolved by the caller (see the fallback chain in
    app/tasks/catalog_sync.py).
  - "price" ("<amount> <ISO>", e.g. "50000.00 TZS"), "availability" (exact
    "in stock"/"out of stock"), "condition", "description", "link" were
    already spec-correct.
  PROVEN LIVE on the staging catalog (E1): see the E1 proof notes on
  upsert_items/delete_items below.
  REMAINING STAGING ASSUMPTIONS (accepted shapes Meta has not yet had to
  exercise, so still unproven):
  - "description" falls back to the product name when Product.description is
    unset (Meta requires a description for most verticals).
  - Relative image paths joined onto PUBLIC_BASE_URL are fetchable by Meta.
  - TZS prices render without the decimals being rejected or displayed oddly.
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.security import normalize_phone
from app.db.models import Product

logger = logging.getLogger(__name__)

CURRENCY = "TZS"

IN_STOCK = "in stock"
OUT_OF_STOCK = "out of stock"

# Product has no condition column; everything sold here is new retail stock.
CONDITION_NEW = "new"

# Commerce-vertical catalogs take PRODUCT_ITEM (proven live: items_batch
# rejects the batch with #100 when item_type is missing).
ITEM_TYPE_PRODUCT = "PRODUCT_ITEM"

# Fields requested when READING a merchant's catalog for shop import
# (list_products). STAGING-UNVERIFIED: no management-scoped token exists
# in dev, so these names come from the Catalog Product reference, not a
# live call — catalog_item_to_product_data parses everything defensively
# (.get with fallbacks) so an unknown/missing field skips one item with a
# reason instead of failing the import.
CATALOG_PRODUCT_FIELDS = "id,title,description,price,availability,image_url,link,brand"

# Hard cap on items pulled per import — bounds one merchant's click to a
# manageable batch; the response reports truncated=true past this.
IMPORT_MAX_ITEMS = 1000


class CatalogError(Exception):
    """Any Meta catalog failure: transport, HTTP status, or Meta error payload."""


class CatalogMappingError(CatalogError):
    """A Product row cannot be represented as a catalog item (bad image/phone)."""


def format_price_tzs(amount: float) -> str:
    """
    Meta "<amount> <ISO>" price format, e.g. "50000.00 TZS" — matches the
    documented example shape ("100.00 USD"). Two decimals always, even for
    whole-shilling prices; STAGING: confirm TZS renders without decimals
    being rejected or displayed oddly.
    """
    return f"{float(amount):.2f} {CURRENCY}"


def _resolve_image_url(product: Product) -> str:
    """
    Meta must be able to fetch the image from the public internet, so only an
    absolute https URL is usable as-is. Product images are stored per
    app/api/v1/products.py upload_product_image: absolute R2 public URLs when
    R2 is configured, else a relative local "/static/..." path — which Meta
    can never fetch. Relative paths are joined onto PUBLIC_BASE_URL (which
    must therefore be the publicly reachable base, never localhost); a
    missing image or an unjoinable relative path raises instead of sending a
    feed Meta would reject.
    """
    image_url = (product.image_url or "").strip()
    if not image_url:
        raise CatalogMappingError(
            f"Product {product.id} has no image_url; Meta requires an image."
        )
    if image_url.startswith(("http://", "https://")):
        return image_url
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        raise CatalogMappingError(
            f"Product {product.id} has a relative image_url ({image_url!r}) but "
            "PUBLIC_BASE_URL is unset, so no public URL can be built."
        )
    return f"{base}/{image_url.lstrip('/')}"


def _merchant_chat_link(product_name: str, merchant_phone: Optional[str]) -> str:
    """
    wa.me deep link into the merchant's WhatsApp chat with a product prefill.
    There are no storefront pages in this system, so the catalog item links
    straight to the merchant conversation instead of a product URL — Meta
    accepts any https link here, and the chat IS the storefront.
    """
    digits = normalize_phone(merchant_phone or "")
    if not digits:
        raise CatalogMappingError(
            "merchant_phone is required to build the catalog item link."
        )
    prefill = quote(f"Hi, I'm interested in {product_name}")
    return f"https://wa.me/{digits}?text={prefill}"


def product_to_catalog_item(
    product: Product,
    merchant_phone: Optional[str],
    merchant_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pure mapping from a Product row to Meta PRODUCT_ITEM data.

    merchant_phone is the merchant's WhatsApp number (User.username, falling
    back to User.phone_number per app/tasks/usage_alerts.py) — passed
    explicitly so this stays pure with no lazy relationship loads. It builds
    the wa.me chat link only.

    merchant_name is the merchant's display name for the Required "brand"
    field, resolved by the caller (see the fallback chain in
    app/tasks/catalog_sync.py: User.business_name, first
    WhatsAppAccount.name, User.name, then the merchant_phone digits). A
    missing name raises instead of sending an item Meta would reject.

    min_price and the negotiation floor are NEVER included: the catalog only
    ever carries the public max_price. cost_price stays out for the same
    reason.
    """
    # C1 sellability rule, mirrored from _is_out_of_stock (app/services/llm.py):
    # quantity < 1 or merchant-deactivated means out of stock. Duplicated
    # (not imported) on purpose — app/core must not import app/services, and
    # a legacy is_active=None still counts as in-stock here, exactly as there.
    sellable = not ((product.quantity or 0) < 1 or product.is_active is False)
    brand = (merchant_name or "").strip()
    if not brand:
        raise CatalogMappingError(
            "merchant_name is required: Meta requires a brand on every item."
        )
    return {
        "id": str(product.id),
        # Meta caps title at 100 chars; truncate defensively rather than
        # letting one long product name fail the whole batch.
        "title": (product.name or "")[:100],
        # Meta requires a description for most verticals; fall back to the
        # (full, untruncated) name rather than omitting it (see module docstring).
        "description": product.description or product.name,
        "price": format_price_tzs(product.max_price),
        "availability": IN_STOCK if sellable else OUT_OF_STOCK,
        "condition": CONDITION_NEW,
        # PRODUCT_ITEM takes an array of {"url": ...} objects, not a bare
        # image_url string — one entry, no tags.
        "image": [{"url": _resolve_image_url(product)}],
        "link": _merchant_chat_link(product.name, merchant_phone),
        "brand": brand,
    }


def parse_catalog_price(value: Any) -> float:
    """
    Inverse of format_price_tzs: Meta "<amount> <ISO>" (e.g. "50000.00 TZS")
    back to a float. Anything malformed — or any currency that isn't TZS —
    raises: Product prices are unitless TZS everywhere downstream (see the
    $100,000 production incident in CLAUDE.md), so importing a foreign
    amount as shillings would be a silent ~1000x error.
    """
    if not isinstance(value, str):
        raise CatalogMappingError(f"price is not a string: {value!r}")
    parts = value.strip().split()
    if len(parts) != 2:
        raise CatalogMappingError(f"unparseable price: {value!r}")
    amount_str, currency = parts
    if currency.upper() != CURRENCY:
        raise CatalogMappingError(
            f"price currency is {currency}, not {CURRENCY}: {value!r}"
        )
    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        raise CatalogMappingError(f"unparseable price: {value!r}") from None
    if amount < 0:
        raise CatalogMappingError(f"negative price: {value!r}")
    return amount


def catalog_item_to_product_data(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure mapping from a merchant catalog item (list_products shape) to
    Product-create kwargs. Inverse of product_to_catalog_item.

    Deliberately conservative (see #73): min_price == max_price (firm
    until the merchant edits — never auto-discount their goods), in-stock
    items get quantity 1 (safe against overselling; correct upward in the
    dashboard stepper), out-of-stock items get quantity 0 + inactive.
    The catalog item id is kept as sku ("igshop:<id>") so re-imports match
    instead of duplicating. Images are hotlinked, never copied to R2.
    Unmappable items raise CatalogMappingError with a human-readable
    reason — the importer records it as a skip, not a failure.
    """
    if not isinstance(item, dict):
        raise CatalogMappingError(f"catalog item is not an object: {item!r}")
    item_id = item.get("id")
    title = (item.get("title") or "").strip()
    if not item_id or not title:
        raise CatalogMappingError(
            f"item is missing {'id' if not item_id else 'title'}: {item!r}"
        )
    price = parse_catalog_price(item.get("price"))
    in_stock = (item.get("availability") or "").strip().lower() == IN_STOCK
    image_url = (item.get("image_url") or "").strip() or None
    return {
        "name": title,
        "description": (item.get("description") or "").strip() or None,
        "max_price": price,
        "min_price": price,
        "quantity": 1 if in_stock else 0,
        "is_active": in_stock,
        "image_url": image_url,
        "sku": f"igshop:{item_id}",
    }


class MetaCatalogClient:
    """
    Thin wrapper over the Commerce catalog items_batch + batch-status
    endpoints, plus read access for shop import. Constructed with
    (catalog_id, access_token), same shape as
    WhatsAppProvider/InstagramProvider.
    """

    def __init__(self, catalog_id: str, access_token: str):
        self.catalog_id = catalog_id
        self.access_token = access_token
        self.base_url = (
            f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{catalog_id}"
        )
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _raise_for_meta_error(self, payload: Any, action: str) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise CatalogError(f"{action} returned a non-JSON payload: {payload!r}")
        error = payload.get("error")
        if error:
            raise CatalogError(f"{action} failed: {error}")
        return payload

    async def _post_batch(self, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"{self.base_url}/items_batch"
        payload: Dict[str, Any] = {
            "requests": requests,
            "allow_upsert": True,
            "item_type": ITEM_TYPE_PRODUCT,
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=self.headers)
        except httpx.HTTPError as e:
            raise CatalogError(f"items_batch request failed: {e}") from e
        if response.status_code >= 400:
            raise CatalogError(
                f"items_batch failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            data = response.json()
        except ValueError as e:
            raise CatalogError(f"items_batch returned invalid JSON: {e}") from e
        return self._raise_for_meta_error(data, "items_batch")

    async def upsert_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Creates or updates items in one batch. With allow_upsert=true, CREATE
        requests upsert on id, so updates need no separate method.
        Returns Meta's response (batch handles for status polling).
        E1 PROVEN (staging 2026-10-28): a TEST upsert of the corrected shape
        finished with errors_total_count 0, no errors/warnings/invalid ids.
        """
        if not items:
            raise CatalogError("upsert_items needs at least one item.")
        requests = [{"method": "CREATE", "data": item} for item in items]
        return await self._post_batch(requests)

    async def delete_items(self, retailer_ids: List[str]) -> Dict[str, Any]:
        """
        Deletes items by id in one batch. The parameter keeps the
        retailer_ids name (and callers keep passing str(Product.id)) because
        that same value is what D3 messaging sends as product_retailer_id —
        only the catalog wire key changed, from retailer_id to id.
        E1 PROVEN (staging 2026-10-28): DELETE with {"id": ...} inside data
        finished with errors_total_count 0 — the retailer_id-at-request-level
        alternative was not needed.
        """
        if not retailer_ids:
            raise CatalogError("delete_items needs at least one retailer_id.")
        requests = [
            {"method": "DELETE", "data": {"id": rid}} for rid in retailer_ids
        ]
        return await self._post_batch(requests)

    async def _get(self, path: str, params: Dict[str, Any], action: str) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, headers=self.headers)
        except httpx.HTTPError as e:
            raise CatalogError(f"{action} failed: {e}") from e
        if response.status_code >= 400:
            raise CatalogError(
                f"{action} failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            data = response.json()
        except ValueError as e:
            raise CatalogError(f"{action} returned invalid JSON: {e}") from e
        return self._raise_for_meta_error(data, action)

    async def get_batch_status(self, handle: str) -> Dict[str, Any]:
        """
        Single status poll for an items_batch handle. Returns Meta's payload
        as-is (per-item errors surface here, not on the POST); callers poll
        until the batch finishes. Raises CatalogError on any failure.
        """
        return await self._get(
            "check_batch_request_status", {"handle": handle}, "check_batch_request_status"
        )

    async def list_products(
        self, limit: int = 100, after: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        One page of catalog items for shop import. Returns Meta's payload
        as-is ({"data": [...], "paging": {...}}); callers follow
        paging.cursors.after until exhausted. Raises CatalogError on any
        failure — including the permission error a merchant sees before
        granting Taskflow partner access to their catalog.
        """
        params: Dict[str, Any] = {"fields": CATALOG_PRODUCT_FIELDS, "limit": limit}
        if after:
            params["after"] = after
        return await self._get("products", params, "catalog products")
