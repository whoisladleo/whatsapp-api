from sqlalchemy import func
from sqlalchemy.orm import Session
from app.db.models import Product, User
from app.schemas.product import ProductCreate, ProductUpdate
from typing import List, Optional
import re
import uuid


def lock_merchant_row(db: Session, merchant_id: uuid.UUID) -> User:
    """
    Row-locks the merchant's User row for the surrounding transaction
    (SELECT ... FOR UPDATE on Postgres; silently a plain SELECT on
    SQLite, which serializes writers at the database level instead).
    Product rows can't be the lock target — the racy ones don't exist
    yet — so the merchant row is the stable per-merchant serialization
    point. Callers must hold no other row locks (lock ordering is trivially
    safe: this is the only FOR UPDATE in the codebase).
    """
    return db.query(User).filter(User.id == merchant_id).with_for_update().one()

def generate_slug(db: Session, name: str) -> str:
    base_slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    if not base_slug:
        base_slug = str(uuid.uuid4())[:8]
    
    slug = base_slug
    # Check for collision
    while db.query(Product).filter(Product.slug == slug).first():
        slug = f"{base_slug}-{str(uuid.uuid4())[:4]}"
    return slug

def get_product_by_id(db: Session, product_id: uuid.UUID, merchant_id: uuid.UUID) -> Optional[Product]:
    return db.query(Product).filter(
        Product.id == product_id,
        Product.merchant_id == merchant_id
    ).first()

def get_products_by_merchant(db: Session, merchant_id: uuid.UUID) -> List[Product]:
    return db.query(Product).filter(Product.merchant_id == merchant_id).all()

def create_product(db: Session, product_in: ProductCreate, merchant_id: uuid.UUID) -> Product:
    slug = generate_slug(db, product_in.name)
    db_product = Product(
        **product_in.model_dump(),
        slug=slug,
        merchant_id=merchant_id
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    # Best-effort Meta catalog sync: silent no-op when CATALOG_* is unset,
    # never breaks this write (see app/tasks/catalog_sync.py).
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(db_product.id, "upsert")
    return db_product

def update_product(db: Session, db_product: Product, product_in: ProductUpdate) -> Product:
    update_data = product_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_product, field, value)

    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(db_product.id, "upsert")
    return db_product

def update_product_stock(
    db: Session,
    db_product: Product,
    quantity: Optional[int] = None,
    is_active: Optional[bool] = None,
) -> Product:
    # Narrow write: only stock fields. None means "leave unchanged"
    # (covers both omitted and explicit-null).
    if quantity is not None:
        db_product.quantity = quantity
    if is_active is not None:
        db_product.is_active = is_active

    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(db_product.id, "upsert")
    return db_product

def delete_product(db: Session, db_product: Product) -> None:
    retailer_id = str(db_product.id)
    db.delete(db_product)
    db.commit()
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(retailer_id, "delete")

def bulk_upsert_products(db: Session, merchant_id: uuid.UUID, products_data: List[dict], dry_run: bool = False) -> dict:
    """
    products_data is a list of dictionaries.
    Uses 'sku' to identify existing products.
    """
    stats = {
        "requires_confirmation": dry_run,
        "total_products": len(products_data),
        "new_products_count": 0,
        "duplicate_products_count": 0,
        "duplicate_skus": []
    }

    # Rows actually written, for the catalog sync below (dry runs write
    # nothing, so there is nothing to sync).
    touched_ids = []
    new_products = []

    for p_data in products_data:
        sku = p_data.get("sku")
        db_product = None
        if sku:
            db_product = db.query(Product).filter(
                Product.merchant_id == merchant_id,
                Product.sku == str(sku)
            ).first()
            
        if db_product:
            stats["duplicate_products_count"] += 1
            if sku:
                stats["duplicate_skus"].append(sku)
            if not dry_run:
                for key, value in p_data.items():
                    if hasattr(db_product, key) and key not in ['id', 'merchant_id', 'slug', 'created_at']:
                        setattr(db_product, key, value)
                db.add(db_product)
                touched_ids.append(db_product.id)
        else:
            stats["new_products_count"] += 1
            if not dry_run:
                name = p_data.get("name", "Unnamed")
                slug = generate_slug(db, name)
                new_product = Product(
                    merchant_id=merchant_id,
                    slug=slug,
                    **p_data
                )
                db.add(new_product)
                new_products.append(new_product)

    if not dry_run:
        db.commit()
        for new_product in new_products:
            touched_ids.append(new_product.id)
        from app.tasks.catalog_sync import dispatch_catalog_sync
        for touched_id in touched_ids:
            dispatch_catalog_sync(touched_id, "upsert")

    return stats

def import_catalog_products(db: Session, merchant_id: uuid.UUID, products_data: List[dict]) -> dict:
    """
    Persists items already mapped by catalog_item_to_product_data (each has
    name + sku "igshop:<catalog-item-id>"). Unlike bulk_upsert_products,
    matches NEVER update: a sku match (same catalog item re-imported) or a
    case-insensitive name match (merchant already hand-entered it) skips
    the row, since merchant-tuned prices/floors must not be overwritten by
    a re-import. Always writes (no dry run) and syncs created rows to the
    Taskflow catalog like every other product write path.

    The whole match-then-insert runs under the merchant's row lock: two
    simultaneous imports for one merchant would otherwise both miss each
    other's uncommitted rows and insert duplicates (there is no DB
    uniqueness on sku/name to catch it). The second import blocks here
    until the first commits, then sees its rows and skips. The slow Meta
    fetch stays outside the lock (in the endpoint) — only match+insert
    holds it.
    """
    lock_merchant_row(db, merchant_id)
    stats = {
        "total_items": len(products_data),
        "imported_count": 0,
        "skipped_count": 0,
        "skipped": [],
    }

    new_products = []
    # Keys already taken by earlier rows in THIS batch — the batch commits
    # once at the end, so the DB lookups below can't see them yet.
    seen_skus = set()
    seen_names = set()
    for p_data in products_data:
        name = (p_data.get("name") or "").strip()
        sku = p_data.get("sku")
        db_product = None
        reason = None
        if sku and str(sku) in seen_skus:
            reason = "duplicate item in this import"
        elif name and name.lower() in seen_names:
            reason = "duplicate name in this import"
        if reason:
            stats["skipped_count"] += 1
            stats["skipped"].append({"name": name or sku, "reason": reason})
            continue
        if sku:
            db_product = db.query(Product).filter(
                Product.merchant_id == merchant_id,
                Product.sku == str(sku)
            ).first()
            if db_product:
                reason = "already imported"
        if not db_product and name:
            db_product = db.query(Product).filter(
                Product.merchant_id == merchant_id,
                func.lower(Product.name) == name.lower()
            ).first()
            if db_product:
                reason = "a product with this name already exists"

        if db_product:
            stats["skipped_count"] += 1
            stats["skipped"].append({"name": name or sku, "reason": reason})
            continue

        stats["imported_count"] += 1
        if sku:
            seen_skus.add(str(sku))
        if name:
            seen_names.add(name.lower())
        db_product = Product(
            merchant_id=merchant_id,
            slug=generate_slug(db, name),
            **p_data
        )
        db.add(db_product)
        new_products.append(db_product)

    db.commit()
    from app.tasks.catalog_sync import dispatch_catalog_sync
    for new_product in new_products:
        dispatch_catalog_sync(new_product.id, "upsert")

    return stats
