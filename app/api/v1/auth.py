import secrets
import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from datetime import timedelta, datetime

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.core.rate_limit import check_and_increment
from app.core.security import normalize_phone
from app.schemas.auth import (
    Token,
    OTPRequest,
    OTPVerify,
    FacebookExchangeRequest,
    UserProfile,
    UserProfileUpdate,
    MySubscriptionRead,
    PayNowRequest,
    PayNowResponse,
)
from app.crud import user as crud_user
from app.core.provider import WhatsAppProvider
from app.services import facebook_oauth, self_service_billing

router = APIRouter()

# The /callback redirect only carries a short-lived login code (exchanged
# for the real session token by /exchange) rather than the token itself, so
# a long-lived credential never ends up in a URL — browser history, server
# access logs, etc.
FACEBOOK_LOGIN_CODE_EXPIRE_MINUTES = 2

# Kept in sync with WHATSAPP_OTP_TEMPLATE_LANGUAGE so the plain-text
# fallback (sent when the template send fails, or is unconfigured) is in
# the same language as the template would have been — there's no per-user
# language signal available this early (a login OTP request has no prior
# conversation history to infer one from), so this is a single global
# default, not per-customer. Falls back to English for any configured
# language this dict doesn't have a translation for.
_OTP_FALLBACK_MESSAGES = {
    "sw": "OTP yako wa kuingia Taskflow OS ni: *{otp}*. Ni siri, usimpe mtu. Unaisha ndani ya dakika 5.",
    "en": "Your Taskflow OS login OTP is: *{otp}*. Keep it secret. Expires in 5 minutes.",
}


def _otp_fallback_message(otp: str) -> str:
    template = _OTP_FALLBACK_MESSAGES.get(
        settings.WHATSAPP_OTP_TEMPLATE_LANGUAGE, _OTP_FALLBACK_MESSAGES["en"]
    )
    return template.format(otp=otp)


async def send_otp_message(phone_number: str, otp: str):
    provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)

    if settings.WHATSAPP_OTP_TEMPLATE_NAME:
        try:
            response = await provider.send_template(
                to=phone_number,
                name=settings.WHATSAPP_OTP_TEMPLATE_NAME,
                language_code=settings.WHATSAPP_OTP_TEMPLATE_LANGUAGE,
                components=[
                    {"type": "body", "parameters": [{"type": "text", "text": otp}]},
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": 0,
                        "parameters": [{"type": "text", "text": otp}],
                    },
                ],
            )
            if response and "messages" in response:
                return
            print(f"OTP template send rejected for {phone_number}, falling back to text: {response}")
        except Exception as e:
            print(f"OTP template send failed for {phone_number}, falling back to text: {e}")

    # Fallback: a plain-text OTP only reaches the customer if they've
    # messaged us in the last 24h, but it's better than nothing while the
    # template is pending Meta's review (or if it ever gets rejected).
    message = _otp_fallback_message(otp)
    try:
        await provider.send_text(to=phone_number, body=message)
    except Exception as e:
        print(f"Failed to send OTP to {phone_number}: {e}")

@router.post("/request-otp")
async def request_otp(
    otp_data: OTPRequest, 
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    phone_number = normalize_phone(otp_data.username.strip())

    if not check_and_increment(f"otp_req:{phone_number}", limit=3, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait a few minutes and try again.",
        )

    # Pre-onboarding: Create user if they don't exist. A phone number that
    # doesn't match any User.username but does match User.phone_number
    # belongs to a Facebook-login merchant who's already self-reported it
    # (PATCH /auth/me, Pay Now) — promote it to username rather than
    # creating a second, unlinked User for the same person (see issue #7).
    user = crud_user.get_user_by_username(db, phone_number)
    if not user:
        candidate = crud_user.get_user_by_phone_number(db, phone_number)
        # Only promote a phone_number match that has no username of its own
        # — a candidate that already has one is a distinct, already-formed
        # account that just happens to share this phone_number value, not
        # the same unlinked identity, and must never be silently hijacked.
        if candidate and candidate.username is None:
            user = crud_user.promote_phone_number_to_username(db, candidate, phone_number)
        else:
            user = crud_user.create_user(db, username=phone_number)

    # Generate OTP
    otp_code = security.generate_otp(6)
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    
    # Save OTP to user
    crud_user.set_user_otp(db, user, otp_code, expires_at)
    
    # Dispatch WhatsApp message in background
    background_tasks.add_task(send_otp_message, phone_number, otp_code)
    
    return {"message": "OTP sent successfully"}

@router.post("/verify-otp", response_model=Token)
def verify_otp(
    otp_data: OTPVerify,
    db: Session = Depends(get_db)
):
    phone_number = normalize_phone(otp_data.username.strip())

    if not check_and_increment(f"otp_verify:{phone_number}", limit=5, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please request a new OTP.",
        )

    user = crud_user.get_user_by_username(db, phone_number)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
        
    if not user.otp_code or user.otp_code != otp_data.otp_code:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OTP code",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    if user.otp_expires_at and user.otp_expires_at < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OTP code has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    # Clear OTP after successful verification
    crud_user.clear_user_otp(db, user)
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }


@router.get("/facebook/start")
async def start_facebook_login():
    """
    An independent alternative to WhatsApp OTP login (not account linking —
    see app/db/models.py's User.facebook_id), added because OTP delivery
    has been unreliable. No current_user dependency: unlike Instagram's
    oauth/start, this endpoint IS the login mechanism, so there's no
    existing session to require yet.
    """
    state = secrets.token_urlsafe(16)
    return {"authorize_url": facebook_oauth.build_authorize_url(state)}


@router.get("/facebook/callback")
async def facebook_login_callback(
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    login_url = f"{settings.DASHBOARD_URL}/login"

    if error or not code:
        return RedirectResponse(f"{login_url}?fb_error=1")

    try:
        access_token = await facebook_oauth.exchange_code_for_token(code)
        facebook_id, name, email = await facebook_oauth.fetch_profile(access_token)
    except facebook_oauth.FacebookOAuthError:
        return RedirectResponse(f"{login_url}?fb_error=1")

    user = crud_user.get_user_by_facebook_id(db, facebook_id)
    if not user:
        user = crud_user.create_facebook_user(db, facebook_id, name, email)

    login_code = security.create_access_token(
        user.id, expires_delta=timedelta(minutes=FACEBOOK_LOGIN_CODE_EXPIRE_MINUTES)
    )
    return RedirectResponse(f"{login_url}?fb_code={login_code}")


@router.post("/facebook/exchange", response_model=Token)
def exchange_facebook_login_code(payload: FacebookExchangeRequest, db: Session = Depends(get_db)):
    """Trades the short-lived code from /facebook/callback for a real session token."""
    try:
        decoded = jwt.decode(payload.code, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = uuid.UUID(decoded["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired login code")

    user = crud_user.get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }


@router.get("/me", response_model=UserProfile)
def get_my_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserProfile)
def update_my_profile(
    payload: UserProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Currently only phone_number is settable — self-reported billing contact
    for a Facebook-login merchant (who has no phone tied to their account
    any other way), matched against the Customer who paid for the platform
    subscription. See app/services/subscription_access.py.
    """
    if payload.phone_number is not None:
        normalized = normalize_phone(payload.phone_number)
        existing = crud_user.get_user_by_phone_number(db, normalized)
        if existing and existing.id != current_user.id:
            raise HTTPException(status_code=400, detail="This phone number is already in use.")
        crud_user.set_user_phone_number(db, current_user, normalized)

    return current_user


@router.get("/me/subscription", response_model=MySubscriptionRead)
def get_my_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Self-service status view — see app/services/self_service_billing.py."""
    subscription = self_service_billing.get_my_subscription(db, current_user)
    if not subscription:
        return MySubscriptionRead()

    return MySubscriptionRead(
        status=subscription.status.value,
        monthly_amount=subscription.monthly_amount,
        next_billing_at=subscription.next_billing_at,
        retry_count=subscription.retry_count,
        past_due_since=subscription.past_due_since,
    )


@router.post("/me/subscription/pay", response_model=PayNowResponse)
async def pay_now(
    payload: PayNowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Dashboard-native Pay Now for Taskflow's own subscription — same flow
    regardless of whether this is a first payment, catching up a lapsed
    one, or resubscribing after cancellation. See
    app/services/self_service_billing.py.
    """
    try:
        result = await self_service_billing.pay_now(db, current_user, payload.phone_number)
    except self_service_billing.NoPlatformProductConfiguredError:
        raise HTTPException(status_code=503, detail="Subscription billing isn't configured yet.")
    except self_service_billing.PhoneNumberRequiredError:
        raise HTTPException(status_code=400, detail="A phone number is required to pay.")
    except self_service_billing.PhoneNumberInUseError:
        raise HTTPException(status_code=400, detail="This phone number is already in use.")
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to start payment. Please try again.")

    return result
