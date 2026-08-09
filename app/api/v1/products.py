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
from app.schemas.product import ProductCreate, ProductUpdate, ProductResponse
from app.crud.product import (
    create_product, get_product_by_id, get_products_by_merchant,
    update_product, delete_product, bulk_upsert_products
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

@router.get("/bulk/template")
def download_csv_template():
    csv_content = "name,description,max_price,min_price,quantity,category,sku,barcode,cost_price,weight,image_url\n"
    csv_content += "Sample Product,A great product,50000,45000,10,Electronics,SKU-001,123456789,30000,1.5,https://example.com/image.jpg\n"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products_template.csv"}
    )

@router.post("/bulk/upload", status_code=status.HTTP_200_OK)
def upload_csv_bulk(
    confirm: bool = False,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")
        
    content = file.file.read().decode('utf-8')
    csv_reader = csv.DictReader(io.StringIO(content))
    
    products_data = []
    for row in csv_reader:
        # Clean and convert numeric fields
        try:
            row['max_price'] = float(row['max_price']) if row.get('max_price') else 0.0
            row['min_price'] = float(row['min_price']) if row.get('min_price') else 0.0
            row['quantity'] = int(row['quantity']) if row.get('quantity') else 0
            row['cost_price'] = float(row['cost_price']) if row.get('cost_price') else None
            row['weight'] = float(row['weight']) if row.get('weight') else None
            products_data.append(row)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid numeric data in CSV")
            
    stats = bulk_upsert_products(
        db=db, 
        merchant_id=current_user.id, 
        products_data=products_data,
        dry_run=not confirm
    )
    
    if not confirm:
        return stats
        
    return {"message": "Successfully processed products", "stats": stats}

@router.post("/{product_id}/image", response_model=ProductResponse)
def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_product = get_product_by_id(db=db, product_id=product_id, merchant_id=current_user.id)
    if not db_product:
        raise HTTPException(status_code=404, detail="Product not found")
        
    # Ensure upload directory exists
    upload_dir = "static/uploads/products"
    os.makedirs(upload_dir, exist_ok=True)
    
    # Save the file
    file_extension = file.filename.split(".")[-1]
    filename = f"{current_user.id}_{product_id}_{uuid.uuid4().hex[:8]}.{file_extension}"
    file_path = os.path.join(upload_dir, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Update product image_url
    image_url = f"/static/uploads/products/{filename}"
    db_product.image_url = image_url
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    
    return db_product
