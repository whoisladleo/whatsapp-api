from pydantic import BaseModel
from typing import Optional
from datetime import datetime

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

class ProductResponse(ProductBase):
    id: int
    slug: str
    merchant_id: int
    created_at: datetime

    class Config:
        from_attributes = True
