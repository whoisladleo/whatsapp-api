import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.db.models import User
from app.schemas.account import AccountCreate, AccountUpdate, AccountResponse
from app.crud.account import (
    create_account, get_account_by_id, get_accounts_by_user,
    update_account, delete_account
)
from app.api.deps import get_current_user
from app.core.config import settings

router = APIRouter()

async def validate_whatsapp_credentials(phone_number_id: str, access_token: str) -> bool:
    """Validate WhatsApp credentials by making a request to the Graph API"""
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    async with httpx.AsyncClient() as client:
        try:
            # A simple GET request to the phone number ID endpoint will verify both the token and the ID
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return True
            return False
        except Exception:
            return False

@router.post("/", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_new_account(
    account_in: AccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    is_valid = await validate_whatsapp_credentials(account_in.phone_number_id, account_in.access_token)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Meta Phone Number ID or Access Token."
        )
    return create_account(db=db, account_in=account_in, user_id=current_user.id)

@router.get("/", response_model=List[AccountResponse])
def read_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_accounts_by_user(db=db, user_id=current_user.id)

@router.put("/{account_id}", response_model=AccountResponse)
async def update_existing_account(
    account_id: int,
    account_in: AccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    # If they are updating credentials, validate them
    phone_id_to_check = account_in.phone_number_id or db_account.phone_number_id
    token_to_check = account_in.access_token or db_account.access_token
    
    if account_in.phone_number_id is not None or account_in.access_token is not None:
        is_valid = await validate_whatsapp_credentials(phone_id_to_check, token_to_check)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Meta Phone Number ID or Access Token."
            )
            
    return update_account(db=db, db_account=db_account, account_in=account_in)
