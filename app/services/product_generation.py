"""
AI product drafting (#90): turns a merchant's free-text summary ("I sell
used iPhones, 11 through 14, from 450k") into draft Product rows for
review in the dashboard. Drafts are never customer-visible and never
sync to the Meta catalog until a valid save completes them (see
maybe_complete_draft) — the approval step is structural, not a policy
note. Generated rows can never be subscriptions or plans: those confer
billing and access, so they stay hand-made.
"""
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.crud.product import generate_slug
from app.db.models import Product

logger = logging.getLogger(__name__)

MAX_GENERATED_ITEMS = 10


class GenerationError(Exception):
    """The model returned nothing usable (empty, non-JSON, wrong shape)."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    return text


def parse_generated_items(text: str) -> list[dict]:
    """Pulls the item list out of a model reply. Accepts a bare JSON
    array or {"products": [...]} — anything else raises GenerationError."""
    try:
        data = json.loads(_strip_fences(text or ""))
    except (json.JSONDecodeError, TypeError) as e:
        raise GenerationError(f"Model reply was not JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("products")
    if not isinstance(data, list) or not data:
        raise GenerationError("Model reply contained no product list.")
    return data[:MAX_GENERATED_ITEMS]


def _clean_item(raw: object) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:200]
    if not name:
        return None

    def _price(value: object) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, number)

    max_price = _price(raw.get("max_price"))
    min_price = _price(raw.get("min_price"))
    if min_price > max_price:
        min_price = max_price
    return {
        "name": name,
        "description": str(raw.get("description") or "").strip()[:2000] or None,
        "max_price": max_price,
        "min_price": min_price,
        "category": str(raw.get("category") or "").strip()[:120] or None,
        "sku": str(raw.get("sku") or "").strip()[:80] or None,
    }


GENERATION_SYSTEM_PROMPT = """You turn a shop owner's plain-language summary of what they sell into a product list.
Return ONLY a JSON array (no prose, no fences) of up to %d items. Each item:
{"name": "...", "description": "...", "max_price": <number>, "min_price": <number>, "category": "...", "sku": "..."}
Rules: prices are plain numbers in Tanzanian Shillings (no symbols, no commas); max_price is the asking price, min_price the lowest they'd accept (guess a sensible ~10%% below max when they don't say); description one short sentence; category and sku only when obvious from the summary, otherwise omit them; never invent products they didn't mention.""" % MAX_GENERATED_ITEMS


async def generate_drafts(db: Session, merchant_id, summary: str) -> list[Product]:
    """
    Asks the model for items and persists each as an is_draft Product.
    Raises GenerationError when the reply is unusable, RuntimeError when
    no LLM client is configured. Suggested SKUs are kept only when they
    don't collide with this merchant's existing ones.
    """
    from app.services.llm import llm_service

    if llm_service is None or llm_service.provider is None:
        raise RuntimeError("No LLM client configured.")
    reply = await llm_service.provider.complete(
        system=GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": summary}],
        max_tokens=2048,
    )
    items = [_clean_item(raw) for raw in parse_generated_items(reply)]
    items = [item for item in items if item]
    if not items:
        raise GenerationError("Model reply contained no usable products.")

    existing_skus = {
        row[0]
        for row in db.query(Product.sku)
        .filter(Product.merchant_id == merchant_id, Product.sku.isnot(None))
        .all()
    }
    # Built directly (importer pattern) rather than via create_product:
    # no catalog dispatch for drafts, and is_draft is set from the start.
    drafts = []
    for item in items:
        if item["sku"] in existing_skus:
            item["sku"] = None
        elif item["sku"]:
            existing_skus.add(item["sku"])
        draft = Product(
            merchant_id=merchant_id,
            slug=generate_slug(db, item["name"]),
            name=item["name"],
            description=item["description"],
            max_price=item["max_price"],
            min_price=item["min_price"],
            category=item["category"],
            sku=item["sku"],
            quantity=0,
            is_active=True,
            is_subscription=False,
            is_draft=True,
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        drafts.append(draft)
    return drafts
