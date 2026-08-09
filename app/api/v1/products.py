from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.db.models import User
from app.schemas.product import ProductCreate, ProductUpdate, ProductResponse
from app.crud.product import (
    create_product, get_product_by_id, get_products_by_merchant,
    update_product, delete_product
)
from app.api.deps import get_current_user

router = APIRouter()

@router.post("/", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_new_product(
    product_in: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return create_product(db=db, product_in=product_in, merchant_id=current_user.id)

@router.get("/", response_model=List[ProductResponse])
def read_products(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_products_by_merchant(db=db, merchant_id=current_user.id)

@router.put("/{product_id}", response_model=ProductResponse)
def update_existing_product(
    product_id: int,
    product_in: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
        
    return update_product(db=db, db_product=db_product, product_in=product_in)

@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_existing_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
        
    delete_product(db=db, db_product=db_product)
    return None
