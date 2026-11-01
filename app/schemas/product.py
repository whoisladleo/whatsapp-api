from app.schemas.common import UtcDateTime
import uuid
from pydantic import BaseModel, Field, field_validator, model_validator
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
    # Platform plan SKU (#90). CREATE/UPDATE payloads carrying either field
    # are rejected for non-admins in the product endpoints; responses always
    # carry both so owners can see their plan's meter.
    is_platform_plan: bool = False
    monthly_reply_limit: Optional[int] = None

class ProductCreate(ProductBase):
    # slug is auto-generated
    #
    # Validation lives here (and on ProductUpdate), not on ProductBase:
    # ProductResponse shares the base, and reads of legacy rows must never
    # 500 just because an old row predates these rules. Zeros are valid
    # everywhere — drafts legitimately carry 0 prices/quantity.
    name: str = Field(min_length=1)
    max_price: float = Field(ge=0)
    min_price: float = Field(ge=0)
    quantity: int = Field(default=0, ge=0)
    cost_price: Optional[float] = Field(default=None, ge=0)
    weight: Optional[float] = Field(default=None, ge=0)
    monthly_reply_limit: Optional[int] = Field(default=None, ge=0)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v

    @model_validator(mode="after")
    def _check_price_floor(self):
        if self.min_price > self.max_price:
            raise ValueError("min_price must be <= max_price")
        return self

class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    max_price: Optional[float] = Field(default=None, ge=0)
    min_price: Optional[float] = Field(default=None, ge=0)
    quantity: Optional[int] = Field(default=None, ge=0)
    description: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = Field(default=None, ge=0)
    weight: Optional[float] = Field(default=None, ge=0)
    is_active: Optional[bool] = None
    is_subscription: Optional[bool] = None
    allow_installments: Optional[bool] = None
    installment_mode: Optional[InstallmentMode] = None
    is_platform_plan: Optional[bool] = None
    monthly_reply_limit: Optional[int] = Field(default=None, ge=0)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v

    @model_validator(mode="after")
    def _check_price_floor(self):
        # Partial updates carry only one side — the schema can't see the
        # stored other half, so only the both-present case is checkable here.
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must be <= max_price")
        return self

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


class ProductGenerateRequest(BaseModel):
    # Free-text summary of what the merchant sells — the model turns it
    # into draft rows (#90). Min length keeps one-word prompts (and their
    # hallucinated catalogs) out; max bounds the model call.
    summary: str = Field(min_length=10, max_length=2000)


class ProductResponse(ProductBase):
    id: uuid.UUID
    slug: str
    merchant_id: uuid.UUID
    created_at: UtcDateTime
    # Shop-import drafts (#74) — the dashboard badges these until a valid
    # save auto-completes them. Deliberately absent from ProductCreate /
    # ProductUpdate: merchants can neither create drafts directly nor flip
    # the flag except through the completion rule.
    is_draft: bool = False

    class Config:
        from_attributes = True
