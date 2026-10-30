import uuid
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from app.db.models import InstallmentMode

class ProductBase(BaseModel):
    name: str
    max_price: float
    min_price: float
    quantity: int = 0
    description: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = None
    weight: Optional[float] = None
    is_active: bool = True
    # Opts this product into recurring billing — see
    # app/services/product_subscription.py.
    is_subscription: bool = False
    # Whether Amara may offer installments for this product at all — see
    # Product.allow_installments. installment_mode only has any effect
    # when this and is_subscription are both true (see InstallmentMode).
    allow_installments: bool = True
    installment_mode: InstallmentMode = InstallmentMode.BNPL

class ProductCreate(ProductBase):
    pass
    # slug is auto-generated

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    quantity: Optional[int] = None
    description: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = None
    weight: Optional[float] = None
    is_active: Optional[bool] = None
    is_subscription: Optional[bool] = None
    allow_installments: Optional[bool] = None
    installment_mode: Optional[InstallmentMode] = None

class ProductStockUpdate(BaseModel):
    # Narrow PATCH body for PATCH /products/{id}/stock: only stock fields.
    # None (omitted or explicit null) means "leave unchanged".
    quantity: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None

class CatalogImportRequest(BaseModel):
    # The merchant's own Commerce catalog id (Commerce Manager URL) — read
    # via Taskflow's system-user token once the merchant grants partner
    # access (see CATALOG_IMPORT_SETUP.md).
    catalog_id: str


class ProductResponse(ProductBase):
    id: uuid.UUID
    slug: str
    merchant_id: uuid.UUID
    created_at: datetime
    # Shop-import drafts (#74) — the dashboard badges these until a valid
    # save auto-completes them. Deliberately absent from ProductCreate /
    # ProductUpdate: merchants can neither create drafts directly nor flip
    # the flag except through the completion rule.
    is_draft: bool = False

    class Config:
        from_attributes = True
