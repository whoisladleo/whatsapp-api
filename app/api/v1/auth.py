from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import timedelta, datetime

from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.core.rate_limit import check_and_increment
from app.schemas.auth import Token, OTPRequest, OTPVerify
from app.crud import user as crud_user
from app.core.provider import WhatsAppProvider

router = APIRouter()

async def send_otp_message(phone_number: str, otp: str):
    provider = WhatsAppProvider(settings.WHATSAPP_TOKEN, settings.PHONE_NUMBER_ID)
    message = f"OTP yako wa kuingia Taskflow OS ni: *{otp}*. Ni siri, usimpe mtu. Unaisha ndani ya dakika 5."
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
    phone_number = otp_data.username.strip()

    if not check_and_increment(f"otp_req:{phone_number}", limit=3, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait a few minutes and try again.",
        )

    # Pre-onboarding: Create user if they don't exist
    user = crud_user.get_user_by_username(db, phone_number)
    if not user:
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
    phone_number = otp_data.username.strip()

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
