from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List
import csv
import io
import os
import shutil
import uuid
from app.db.session import get_db
from app.db.models import User
from app.schemas.product import (
    ProductCreate, ProductUpdate, ProductStockUpdate, ProductResponse,
    CatalogImportRequest, ProductGenerateRequest,
)
from app.core.catalog_client import (
    IMPORT_MAX_ITEMS, MetaCatalogClient, catalog_item_to_product_data,
    catalog_item_to_draft_data, CatalogError, CatalogMappingError,
    CatalogDraftableError,
)
from app.core.config import settings
from app.crud.product import (
    ProductHasSubscriptionsError,
    create_product, get_product_by_id, get_products_by_merchant,
    update_product, update_product_stock, delete_product, bulk_upsert_products,
    import_catalog_products,
)
from app.api.deps import get_current_user
from app.core.rate_limit import check_and_increment
from app.services.product_generation import GenerationError, generate_drafts
from app.services.support_access import is_support_operator

router = APIRouter()

# Platform plan SKUs (#90) are admin-managed: only a support operator's
# identities may create, change, or delete them. Ordinary merchants see
# the fields on responses but can never set them.
_PLAN_FIELDS = {"is_platform_plan", "monthly_reply_limit"}

# Product images are served back from /static (or a public R2 URL), so any
# file a browser renders as HTML/SVG becomes stored XSS. Only raster image
# types are accepted; the stored extension is derived from the allowlisted
# content type, never from the client-supplied filename.
_PRODUCT_IMAGE_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}
_PRODUCT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
_PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _require_plan_admin(db: Session, user: User, payload: dict) -> None:
    if _PLAN_FIELDS & payload.keys() and not is_support_operator(db, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform admins can manage plan products",
        )


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_new_product(
    product_in: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    _require_plan_admin(db, current_user, product_in.model_dump(exclude_unset=True))
    return create_product(db=db, product_in=product_in, merchant_id=current_user.id)

@router.post("/generate", response_model=List[ProductResponse], status_code=status.HTTP_201_CREATED)
async def generate_products_from_summary(
    payload: ProductGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 10 generations a day per merchant — each one is a paid model call
    # plus up to 10 draft rows, so this is cost control, not punishment.
    if not check_and_increment(f"product_generate:{current_user.id}", 10, 86400):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI generation limit reached. Try again tomorrow.",
        )
    try:
        return await generate_drafts(db, current_user.id, payload.summary)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation isn't configured.",
        )
    except GenerationError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI returned nothing usable. Try rephrasing your summary.",
        )

@router.get("", response_model=List[ProductResponse])
def read_products(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_products_by_merchant(db=db, merchant_id=current_user.id)

@router.put("/{product_id}", response_model=ProductResponse)
def update_existing_product(
    product_id: uuid.UUID,
    product_in: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    _require_plan_admin(db, current_user, product_in.model_dump(exclude_unset=True))
    return update_product(db=db, db_product=db_product, product_in=product_in)

@router.patch("/{product_id}/stock", response_model=ProductResponse)
def patch_product_stock(
    product_id: uuid.UUID,
    stock_in: ProductStockUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Request-shape error, so it fires before the 404 lookup — same as the
    # automatic 422 pydantic raises for a negative quantity.
    if stock_in.quantity is None and stock_in.is_active is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of quantity or is_active must be provided"
        )

    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    return update_product_stock(
        db=db, db_product=db_product,
        quantity=stock_in.quantity, is_active=stock_in.is_active
    )

@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_existing_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    if db_product.is_platform_plan and not is_support_operator(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform admins can manage plan products",
        )
    try:
        delete_product(db=db, db_product=db_product)
    except ProductHasSubscriptionsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return None

@router.get("/bulk/template")
def download_csv_template():
    csv_content = "name,description,max_price,min_price,quantity,category,sku,barcode,cost_price,weight,image_url\n"
    csv_content += "Sample Product,A great product,50000,45000,10,Electronics,SKU-001,123456789,30000,1.5,https://example.com/image.jpg\n"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products_template.csv"}
    )

_BULK_CSV_MAX_BYTES = 5 * 1024 * 1024
_BULK_CSV_REQUIRED_COLUMNS = {"name"}

# Columns a sheet may write. Anything else in the header — merchant_id, id,
# slug, created_at, is_draft, plan fields, installment_mode (a raw string
# can't become the enum), or a merchant's own extra columns — is dropped and
# counted in stats["ignored_columns"], never a 400: merchants have messy
# sheets, and a stray column must not fail an upload (or, worse, TypeError
# the insert into a 500 as merchant_id/slug used to).
_BULK_CSV_ALLOWED_COLUMNS = {
    "name", "description", "max_price", "min_price", "quantity",
    "category", "sku", "barcode", "cost_price", "weight", "image_url",
    "is_active", "is_subscription", "allow_installments",
}
_BULK_CSV_BOOL_COLUMNS = {"is_active", "is_subscription", "allow_installments"}
_BULK_CSV_TRUE_VALUES = {"true", "1", "yes", "y"}
_BULK_CSV_FALSE_VALUES = {"false", "0", "no", "n"}


def _parse_bulk_number(raw, field: str, row_number: int, *, is_int: bool):
    """Coerces one CSV cell to float/int; blank means 'not given' (None)."""
    text = raw.strip() if isinstance(raw, str) else raw
    if text is None or text == "":
        return None
    try:
        return int(text) if is_int else float(text)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Row {row_number}: invalid numeric value for '{field}': {raw!r}",
        )


def _parse_bulk_row(row: dict, row_number: int) -> dict:
    """
    Filters one CSV row to allowed columns and coerces/validates it.
    Runs before bulk_upsert_products, so the checks below cover both the
    insert and the update branch. Every failure names its row (header is
    row 1, so data starts at row 2).
    """
    clean = {k: v for k, v in row.items() if k in _BULK_CSV_ALLOWED_COLUMNS}

    max_price = _parse_bulk_number(clean.get("max_price"), "max_price", row_number, is_int=False)
    min_price = _parse_bulk_number(clean.get("min_price"), "min_price", row_number, is_int=False)
    quantity = _parse_bulk_number(clean.get("quantity"), "quantity", row_number, is_int=True)
    cost_price = _parse_bulk_number(clean.get("cost_price"), "cost_price", row_number, is_int=False)
    weight = _parse_bulk_number(clean.get("weight"), "weight", row_number, is_int=False)
    # Missing/blank numerics keep their historical defaults: inserts need
    # values for the NOT NULL price columns, and updates keep the old
    # zero-the-missing-cell behavior.
    clean["max_price"] = max_price if max_price is not None else 0.0
    clean["min_price"] = min_price if min_price is not None else 0.0
    clean["quantity"] = quantity if quantity is not None else 0
    clean["cost_price"] = cost_price
    clean["weight"] = weight

    for field in ("max_price", "min_price", "quantity", "cost_price", "weight"):
        value = clean[field]
        if value is not None and value < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Row {row_number}: '{field}' must be >= 0 (got {value})",
            )
    if clean["min_price"] > clean["max_price"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Row {row_number}: min_price ({clean['min_price']}) must be "
                f"<= max_price ({clean['max_price']})"
            ),
        )

    for field in _BULK_CSV_BOOL_COLUMNS:
        if field not in clean:
            continue
        raw = clean[field]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # Blank means 'not given': the insert keeps the model default
            # and the update leaves the stored value alone.
            del clean[field]
            continue
        text = raw.strip().lower() if isinstance(raw, str) else raw
        if text in _BULK_CSV_TRUE_VALUES:
            clean[field] = True
        elif text in _BULK_CSV_FALSE_VALUES:
            clean[field] = False
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Row {row_number}: invalid boolean value for '{field}': {raw!r}",
            )

    return clean


@router.post("/bulk/upload", status_code=status.HTTP_200_OK)
def upload_csv_bulk(
    confirm: bool = False,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    filename = file.filename or ""
    if not filename.lower().endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    # Chunked read with a cap — the whole file must never sit in RAM
    # uncapped (same 5MB pattern as upload_product_image).
    size_bytes = 0
    chunks = []
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        size_bytes += len(chunk)
        if size_bytes > _BULK_CSV_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="CSV file is too large (max 5MB)",
            )
        chunks.append(chunk)

    try:
        content = b"".join(chunks).decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail="CSV file must be UTF-8 encoded"
        )

    csv_reader = csv.DictReader(io.StringIO(content))
    if not csv_reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty")
    missing = _BULK_CSV_REQUIRED_COLUMNS - set(csv_reader.fieldnames)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV is missing required columns: {', '.join(sorted(missing))}",
        )

    ignored_columns = sum(
        1 for header in (csv_reader.fieldnames or [])
        if header not in _BULK_CSV_ALLOWED_COLUMNS
    )
    products_data = [
        _parse_bulk_row(row, row_number)
        for row_number, row in enumerate(csv_reader, start=2)
    ]

    stats = bulk_upsert_products(
        db=db,
        merchant_id=current_user.id,
        products_data=products_data,
        dry_run=not confirm
    )
    stats["ignored_columns"] = ignored_columns
    
    if not confirm:
        return stats
        
    return {"message": "Successfully processed products", "stats": stats}

@router.post("/import/instagram-shop", status_code=status.HTTP_200_OK)
async def import_instagram_shop(
    payload: CatalogImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    One-time import of a merchant's Instagram Shop (Commerce catalog)
    into Taskflow products (#73). The merchant grants Taskflow partner
    access to their catalog out-of-band, then pastes the catalog id here
    (or via the dashboard button). Items missing a title or usable price
    persist as drafts the merchant completes in the dashboard (#74);
    unfixable items (foreign currency, missing id) and already-present
    products are skipped with reasons — the response reports
    imported/drafts/skipped, never partial-fails.
    """
    if not settings.CATALOG_ACCESS_TOKEN:
        raise HTTPException(
            status_code=503, detail="Catalog import isn't configured yet."
        )

    catalog_id = (payload.catalog_id or "").strip()
    if not catalog_id:
        raise HTTPException(status_code=422, detail="catalog_id is required.")

    client = MetaCatalogClient(catalog_id, settings.CATALOG_ACCESS_TOKEN)
    raw_items = []
    after = None
    truncated = False
    try:
        while len(raw_items) < IMPORT_MAX_ITEMS:
            page = await client.list_products(limit=100, after=after)
            data = page.get("data")
            if not isinstance(data, list):
                raise CatalogError(f"catalog products returned no data list: {page!r}")
            raw_items.extend(data)
            after = (page.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not data:
                # An empty page ends the import even if Meta still sends a
                # cursor — otherwise a pathological empty-but-cursored page
                # would loop forever without raw_items ever growing.
                break
        else:
            truncated = True
    except CatalogError as e:
        # Permission-shaped failures (no partner grant yet, wrong catalog
        # id) surface Meta's message so the dashboard can tell the merchant
        # what to fix, instead of a bare 502.
        raise HTTPException(status_code=502, detail=f"Could not read catalog: {e}")

    if len(raw_items) > IMPORT_MAX_ITEMS:
        raw_items = raw_items[:IMPORT_MAX_ITEMS]
        truncated = True

    products_data = []
    drafts_data = []
    mapping_skipped = []
    for item in raw_items:
        try:
            products_data.append(catalog_item_to_product_data(item))
        except CatalogDraftableError:
            # Subclass catch must come first — these persist as drafts.
            drafts_data.append(catalog_item_to_draft_data(item))
        except CatalogMappingError as e:
            label = item.get("title") or item.get("id") if isinstance(item, dict) else None
            mapping_skipped.append({"name": label or "unknown item", "reason": str(e)})

    stats = import_catalog_products(db, current_user.id, products_data, drafts_data)
    stats["skipped_count"] += len(mapping_skipped)
    stats["skipped"].extend(mapping_skipped)
    stats["total_items"] = len(raw_items)
    stats["truncated"] = truncated
    return stats

@router.post("/{product_id}/image", response_model=ProductResponse)
def upload_product_image(
    product_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")

    client_filename = file.filename or ""
    client_extension = (
        client_filename.rsplit(".", 1)[-1].lower()
        if "." in client_filename else ""
    )
    if client_extension not in _PRODUCT_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, PNG, WebP, or GIF images are allowed",
        )
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    stored_extension = _PRODUCT_IMAGE_CONTENT_TYPES.get(content_type)
    if stored_extension is None:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, PNG, WebP, or GIF images are allowed",
        )

    size_bytes = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        size_bytes += len(chunk)
        if size_bytes > _PRODUCT_IMAGE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Image is too large (max 5MB)",
            )
    file.file.seek(0)

    filename = f"{current_user.id}_{product_id}_{uuid.uuid4().hex[:8]}.{stored_extension}"

    from app.core.config import settings
    if settings.R2_ACCOUNT_ID and settings.R2_BUCKET_NAME:
        import boto3
        from botocore.client import Config
        s3 = boto3.client(
            's3',
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            config=Config(signature_version='s3v4'),
        )
        s3.upload_fileobj(
            file.file,
            settings.R2_BUCKET_NAME,
            filename,
            ExtraArgs={'ContentType': content_type}
        )
        image_url = f"{settings.R2_PUBLIC_URL.rstrip('/')}/{filename}"
    else:
        # Fallback to local
        upload_dir = "static/uploads/products"
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        image_url = f"/static/uploads/products/{filename}"
    db_product.image_url = image_url
    db.add(db_product)
    db.commit()
    db.refresh(db_product)

    # A first image can make a previously unsyncable product syncable.
    from app.tasks.catalog_sync import dispatch_catalog_sync
    dispatch_catalog_sync(db_product.id, "upsert")

    return db_product
