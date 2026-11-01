import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app.db.session import get_db
from app.db.models import NumberSubscription, NumberSubscriptionStatus, User
from app.schemas.account import (
    AccountCreate, AccountUpdate, AccountResponse, AccountMetaStatus,
    NumberSubscriptionResponse,
)
from app.crud.account import (
    create_account, get_account_by_id, get_accounts_by_user,
    update_account, delete_account,
)
from app.api.deps import Impersonation, forbid_impersonated, get_current_user, get_impersonation
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
    # Numbers are connected manually for now (see #16) — both fields are
    # required, no more silent pool auto-assign when both are omitted.
    if not account_in.phone_number_id or not account_in.access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide both phone_number_id and access_token.",
        )

    is_valid = await validate_whatsapp_credentials(account_in.phone_number_id, account_in.access_token)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Meta Phone Number ID or Access Token."
        )

    account = create_account(db=db, account_in=account_in, user_id=current_user.id)
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
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Starts a paid, on-demand number purchase (10,000 TZS/month) — numbers
    are otherwise connected manually now (see #16), so this is the one
    remaining self-serve way to get one. Only pushes the Tunzaa payment
    request here; the actual Twilio/Meta purchase runs later, once
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
        refund_status=subscription.refund_status,
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
        refund_status=subscription.refund_status,
    )


@router.post("/{account_id}/number-subscription/mark-refunded", response_model=NumberSubscriptionResponse)
def mark_number_subscription_refunded(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    impersonation: Optional[Impersonation] = Depends(get_impersonation),
):
    """Support marks a MANUAL number-purchase refund done (#112).

    Impersonated-operator sessions only, by design: the merchant must not
    be able to mark their own refund (their own token gets 403), and the
    mask keeps the audit ledger's WRITE row attributing operator +
    target. Only MANUAL flips — NULL means nothing was ever owed (400);
    re-marking SUCCEEDED is an idempotent no-op.
    """
    if impersonation is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Marking a refund is a support action — impersonate the merchant first.",
        )
    db_account = get_account_by_id(db=db, account_id=account_id, user_id=current_user.id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    subscription = (
        db.query(NumberSubscription).filter(NumberSubscription.account_id == db_account.id).first()
    )
    if not subscription:
        raise HTTPException(status_code=404, detail="No number subscription for this account.")
    if subscription.refund_status is None:
        raise HTTPException(status_code=400, detail="No refund is owed on this subscription.")

    subscription.refund_status = "SUCCEEDED"
    db.commit()
    db.refresh(subscription)
    return NumberSubscriptionResponse(
        id=subscription.id,
        status=subscription.status.value,
        monthly_amount=subscription.monthly_amount,
        next_billing_at=subscription.next_billing_at,
        retry_count=subscription.retry_count,
        created_at=subscription.created_at,
        refund_status=subscription.refund_status,
    )
