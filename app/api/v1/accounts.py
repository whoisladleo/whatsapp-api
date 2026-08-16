import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.db.models import User
from app.schemas.account import AccountCreate, AccountUpdate, AccountResponse
from app.crud.account import (
    create_account, get_account_by_id, get_accounts_by_user,
    update_account, delete_account,
    PartialWhatsAppCredentialsError, PhonePoolExhaustedError,
    PoolAccessTokenNotConfiguredError,
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

@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_new_account(
    account_in: AccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Only validate against Meta when the merchant supplied BOTH of their own
    # credentials (bring-your-own-number). Omitting both fields means
    # auto-assign from the pool — there's nothing to validate yet, the pool
    # number is Taskflow's own, already-verified number. Supplying exactly
    # one is an incomplete/invalid combination handled below by
    # create_account raising PartialWhatsAppCredentialsError, which gives a
    # clearer message than a Meta-validation call against a half-empty pair
    # ever could.
    if account_in.phone_number_id and account_in.access_token:
        is_valid = await validate_whatsapp_credentials(account_in.phone_number_id, account_in.access_token)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Meta Phone Number ID or Access Token."
            )

    try:
        account = create_account(db=db, account_in=account_in, user_id=current_user.id)
    except PartialWhatsAppCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide both phone_number_id and access_token, or omit both to auto-assign a number from the pool.",
        )
    except PhonePoolExhaustedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No WhatsApp numbers are currently available in the pool. Try again shortly, or provide your own phone_number_id/access_token.",
        )
    except PoolAccessTokenNotConfiguredError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auto-assign is temporarily unavailable (pool credentials not configured). Provide your own phone_number_id/access_token instead.",
        )
    if not account:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp account with this ID already exists",
        )
    return account

@router.get("", response_model=List[AccountResponse])
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

@router.delete("/{account_id}")
def delete_existing_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    delete_account(db=db, db_account=db_account)
    return {"status": "deleted"}
