import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.db.models import NumberSubscription, NumberSubscriptionStatus, User
from app.schemas.account import (
    AccountCreate, AccountUpdate, AccountResponse, AccountMetaStatus,
    NumberSubscriptionResponse,
)
from app.crud.account import (
    create_account, get_account_by_id, get_accounts_by_user,
    update_account, delete_account, get_pool_two_step_pin,
    reassign_account_to_pool_number,
    PartialWhatsAppCredentialsError, PhonePoolExhaustedError,
    PoolAccessTokenNotConfiguredError,
)
from app.api.deps import get_current_user
from app.core.config import settings
from app.services import number_billing

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

async def request_display_name_change(phone_number_id: str, access_token: str, display_name: str) -> None:
    """
    Submits a display name for Meta review on a pool-assigned number, so
    customers see the merchant's own business name instead of "Taskflow OS"
    (the name pool numbers are provisioned with by default in Meta Business
    Manager). Meta approval is async — this only submits the request, it
    doesn't wait for approval — so best-effort: a failure here shouldn't
    block account creation, it just leaves the number needing manual
    approval in Meta Business Manager instead.
    """
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, headers=headers, data={"new_display_name": display_name})
        except Exception:
            pass

async def register_phone_number(phone_number_id: str, access_token: str, pin: str) -> None:
    """
    Re-registers a Cloud API number with its two-step verification PIN.
    Meta requires this after a display name change for the new name to
    actually take effect (see request_display_name_change) — without it,
    the change is accepted but never activated. Best-effort: a failure
    here shouldn't block account creation.
    """
    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{phone_number_id}/register"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, headers=headers, json={"messaging_product": "whatsapp", "pin": pin})
        except Exception:
            pass

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

    was_auto_assigned = not (account_in.phone_number_id and account_in.access_token)
    if was_auto_assigned:
        await request_display_name_change(account.phone_number_id, account.access_token, account.name)
        pin = get_pool_two_step_pin(db, account.id)
        if pin:
            await register_phone_number(account.phone_number_id, account.access_token, pin)

    return account

@router.get("", response_model=List[AccountResponse])
def read_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_accounts_by_user(db=db, user_id=current_user.id)

@router.get("/{account_id}/status", response_model=AccountMetaStatus)
async def get_account_meta_status(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Live phone-number status straight from Meta's Graph API — not our own DB state."""
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    url = f"{settings.WHATSAPP_BASE_URL}/{settings.WHATSAPP_VERSION}/{db_account.phone_number_id}"
    headers = {"Authorization": f"Bearer {db_account.access_token}"}
    params = {
        "fields": "verified_name,display_phone_number,quality_rating,code_verification_status,"
        "status,platform_type,new_display_name,new_name_status"
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, params=params)
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to reach Meta")

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch number status from Meta")

    from app.crud.message import get_recent_send_failures

    data = response.json()
    failures = get_recent_send_failures(db, db_account.id)
    data["recent_send_failures"] = failures["count"]
    data["last_send_failure_reason"] = failures["last_reason"]
    data["last_send_failure_at"] = failures["last_failed_at"]
    return data

@router.put("/{account_id}", response_model=AccountResponse)
async def update_existing_account(
    account_id: uuid.UUID,
    account_in: AccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    if account_in.reassign_from_pool:
        try:
            db_account = reassign_account_to_pool_number(db, db_account)
        except PoolAccessTokenNotConfiguredError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auto-assign is temporarily unavailable (pool credentials not configured). Provide your own phone_number_id/access_token instead.",
            )
        except PhonePoolExhaustedError:
            # Free pool numbers are for ops-driven replenishment only — a
            # live Twilio purchase costs real, recurring money, so it's
            # gated behind POST /{account_id}/purchase-number's paid flow
            # instead of happening for free here.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No WhatsApp numbers are currently available in the free pool. "
                    "Use \"Purchase a WhatsApp Number\" to buy one directly, or provide your own "
                    "phone_number_id/access_token instead."
                ),
            )
        await request_display_name_change(db_account.phone_number_id, db_account.access_token, db_account.name)
        pin = get_pool_two_step_pin(db, db_account.id)
        if pin:
            await register_phone_number(db_account.phone_number_id, db_account.access_token, pin)
        # Any other fields in this same request (name, ai_enabled, system_prompt,
        # etc.) still apply normally — just never phone_number_id/access_token,
        # which the frontend never sends alongside reassign_from_pool anyway.
        return update_account(db=db, db_account=db_account, account_in=account_in)

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
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    delete_account(db=db, db_account=db_account)
    return {"status": "deleted"}


@router.post("/{account_id}/purchase-number", response_model=NumberSubscriptionResponse, status_code=status.HTTP_201_CREATED)
async def purchase_number(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Starts a paid, on-demand number purchase (10,000 TZS/month) — the
    explicit, monetized counterpart to the free (but ops-dependent)
    reassign_from_pool path above. Only pushes the Tunzaa payment request
    here; the actual Twilio/Meta purchase runs later, once
    app/api/v1/tunzaa_webhook.py confirms payment (see
    app/services/number_billing.py).
    """
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    existing = (
        db.query(NumberSubscription).filter(NumberSubscription.account_id == db_account.id).first()
    )
    if existing and existing.status in (
        NumberSubscriptionStatus.PENDING_PAYMENT,
        NumberSubscriptionStatus.PROVISIONING,
        NumberSubscriptionStatus.ACTIVE,
        NumberSubscriptionStatus.PAST_DUE,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This account already has a number subscription (status: {existing.status.value}).",
        )

    try:
        subscription = await number_billing.start_number_purchase(db, db_account)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to start payment: {e}",
        )

    return NumberSubscriptionResponse(
        id=subscription.id,
        status=subscription.status.value,
        monthly_amount=subscription.monthly_amount,
        next_billing_at=subscription.next_billing_at,
        retry_count=subscription.retry_count,
        created_at=subscription.created_at,
    )


@router.get("/{account_id}/number-subscription", response_model=NumberSubscriptionResponse)
def get_number_subscription(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lets the dashboard poll purchase/billing status after purchase_number
    kicks off the (up to ~2 minute) async provisioning chain."""
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    subscription = (
        db.query(NumberSubscription).filter(NumberSubscription.account_id == db_account.id).first()
    )
    if not subscription:
        raise HTTPException(status_code=404, detail="No number subscription for this account.")

    return NumberSubscriptionResponse(
        id=subscription.id,
        status=subscription.status.value,
        monthly_amount=subscription.monthly_amount,
        next_billing_at=subscription.next_billing_at,
        retry_count=subscription.retry_count,
        created_at=subscription.created_at,
    )
