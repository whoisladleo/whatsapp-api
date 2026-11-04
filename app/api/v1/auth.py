import secrets
import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from datetime import timedelta, datetime

from app.api.deps import forbid_impersonated, get_current_user, get_impersonation
from app.db.models import SupportAccessKind, User
from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.core.rate_limit import check_and_increment
from app.core.security import normalize_phone
from app.schemas.auth import (
    Token,
    OTPRequest,
    OTPVerify,
    RefreshRequest,
    LogoutRequest,
    InstagramExchangeRequest,
    ImpersonateRequest,
    ImpersonatorRef,
    MerchantListRead,
    SessionRead,
    UserProfile,
    UserProfileUpdate,
    MySubscriptionRead,
    PayNowRequest,
    PayNowResponse,
    TrialGrantRequest,
    TrialRead,
)
from app.crud import instagram as crud_instagram
from app.crud import refresh_token as crud_refresh
from app.crud import user as crud_user
from app.core.provider import WhatsAppProvider
from app.services import instagram_oauth, self_service_billing
from app.services import support_access

router = APIRouter()

# Instagram Login: the /callback redirect only carries a short-lived login
# code (exchanged for the real session token by /exchange) rather than the
# token itself, so a long-lived credential never ends up in a URL — browser
# history, server access logs, etc. Same shape as the removed Facebook
# Login, which solved the same problem (unreliable WhatsApp OTP) before.
INSTAGRAM_LOGIN_CODE_EXPIRE_MINUTES = 2
INSTAGRAM_LOGIN_STATE_EXPIRE_MINUTES = 10

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

def _mint_session_tokens(db: Session, user: User) -> dict:
    """
    A real login's token pair: short-lived access (60 min) plus a rotating
    refresh bearer (7 days, single-use). Only the refresh hash is stored —
    the raw bearer is returned here once and never again.
    """
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    raw_refresh = security.generate_refresh_token()
    crud_refresh.create_refresh_token(
        db,
        user.id,
        security.sha256_hex(raw_refresh),
        datetime.utcnow() + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
        "refresh_token": raw_refresh,
    }


@router.post("/request-otp")
async def request_otp(
    otp_data: OTPRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db)
):
    phone_number = normalize_phone(otp_data.username.strip())
    if not phone_number:
        # Blank or non-numeric input normalizes to "" — reject before the
        # lookup, or every garbage request shares one User(username="")
        # junk row (#83).
        raise HTTPException(status_code=422, detail="Phone number is required")

    # fail_open=False: a Redis outage must deny OTP sends (429), not
    # unlimit them (WhatsApp cost + victim spam).
    if not check_and_increment(f"otp_req:{phone_number}", limit=3, window_seconds=300, fail_open=False):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait a few minutes and try again.",
        )
    # Per-caller-IP guard alongside the per-recipient one above: without it
    # one caller fans out to unlimited distinct numbers (same abuse, same
    # cost). Mirrors ig_login_start's 30/300s shape; fail_open=False for the
    # same reason as the per-recipient guard.
    client_ip = request.client.host if request.client else "unknown"
    if not check_and_increment(f"otp_req_ip:{client_ip}", limit=30, window_seconds=300, fail_open=False):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait a few minutes and try again.",
        )

    # Pre-onboarding: Create user if they don't exist. A phone number that
    # doesn't match any User.username but does match User.phone_number
    # belongs to a merchant who already self-reported it (PATCH /auth/me,
    # Pay Now) — promote it to username rather than creating a second,
    # unlinked User for the same person (see issue #7).
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

    # fail_open=False: a Redis outage must deny guesses (429), not
    # unlimit them.
    if not check_and_increment(f"otp_verify:{phone_number}", limit=5, window_seconds=300, fail_open=False):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please request a new OTP.",
        )

    user = crud_user.get_user_by_username(db, phone_number)

    if not user:
        # Same 401 shape as a bad code — a distinct 404 would let
        # attackers enumerate which phone numbers are registered.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OTP code",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ONE message for wrong and expired alike — a distinct "expired" reply
    # tells an attacker a guessed code was otherwise correct. The hash check
    # always runs (constant-time), so neither case leaks via timing either.
    code_ok = crud_user.verify_user_otp(user, otp_data.otp_code)
    expired = bool(user.otp_expires_at and user.otp_expires_at < datetime.utcnow())
    if expired or not code_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OTP code",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Clear OTP after successful verification
    crud_user.clear_user_otp(db, user)

    return _mint_session_tokens(db, user)


@router.post("/refresh", response_model=Token)
def refresh_session(
    payload: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Single-use rotation: the presented bearer is revoked and a fresh
    access + refresh pair is minted. A revoked, expired, unknown, or
    orphaned (user deleted) bearer all get the same 401 — no oracle.
    Impersonation JWTs can never pass here (nothing hashes to a stored
    row), so impersonated sessions stay short-lived and non-refreshable.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not check_and_increment(f"auth_refresh:{client_ip}", limit=30, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many refresh attempts. Please wait a few minutes and try again.",
        )

    record = crud_refresh.get_refresh_token_by_hash(
        db, security.sha256_hex(payload.refresh_token)
    )
    user = crud_user.get_user(db, record.user_id) if record is not None else None
    if (
        record is None
        or record.revoked_at is not None
        or record.expires_at < datetime.utcnow()
        or user is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    crud_refresh.revoke_refresh_token(db, record)
    return _mint_session_tokens(db, user)


@router.post("/logout")
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    """
    Revokes the presented refresh bearer. Deliberately unauthenticated —
    an expired access token must never block logging out — and always
    succeeds: unknown, already-revoked, or missing tokens just mean
    there's nothing left to revoke.
    """
    if payload.refresh_token:
        record = crud_refresh.get_refresh_token_by_hash(
            db, security.sha256_hex(payload.refresh_token)
        )
        if record is not None and record.revoked_at is None:
            crud_refresh.revoke_refresh_token(db, record)
    return {"success": True}


@router.get("/instagram/start")
async def start_instagram_login(request: Request):
    """
    An independent alternative to WhatsApp OTP login — no current_user
    dependency, since unlike Instagram's channel-connect oauth/start, this
    endpoint IS the login mechanism and there's no session yet.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not check_and_increment(f"ig_login_start:{client_ip}", limit=30, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait a few minutes and try again.",
        )

    # Signed state (not a bare random string): /callback verifies the
    # signature + expiry without any server-side storage, so a forged or
    # replayed state from another flow can't drive this one.
    state = jwt.encode(
        {
            "nonce": secrets.token_urlsafe(16),
            "exp": datetime.utcnow() + timedelta(minutes=INSTAGRAM_LOGIN_STATE_EXPIRE_MINUTES),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    try:
        authorize_url = instagram_oauth.build_login_authorize_url(state)
    except instagram_oauth.InstagramOAuthError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"authorize_url": authorize_url}


@router.get("/instagram/callback")
async def instagram_login_callback(
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """
    Meta redirects the merchant's browser here after the consent screen.
    Finds-or-creates the User by ig_user_id (an IG already connected under
    a phone-based account logs into that same user — that match IS the
    account link), auto-connects the DM channel via upsert_account, and
    redirects to the dashboard with a short-lived login code.
    """
    login_url = f"{settings.DASHBOARD_URL}/login"

    if error or not code or not state:
        return RedirectResponse(f"{login_url}?ig_error=1")

    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        payload["nonce"]
    except (jwt.InvalidTokenError, KeyError, TypeError):
        return RedirectResponse(f"{login_url}?ig_error=1")

    try:
        short_lived_token = await instagram_oauth.exchange_code_for_short_lived_token(
            code, redirect_uri=settings.INSTAGRAM_LOGIN_REDIRECT_URI
        )
        long_lived_token = await instagram_oauth.exchange_for_long_lived_token(short_lived_token)
        ig_user_id, username = await instagram_oauth.fetch_account_identity(long_lived_token)
    except instagram_oauth.InstagramOAuthError as e:
        print(f"Instagram login exchange failed: {e}")
        return RedirectResponse(f"{login_url}?ig_error=1")

    existing = crud_instagram.get_account_by_ig_business_id(db, ig_user_id)
    if existing is not None and existing.owner is not None:
        user = existing.owner
    else:
        user = crud_user.create_instagram_user(db, username)

    # Same upsert as the channel-connect flow: creates the row for a new
    # merchant, refreshes the token for a returning one, and takes over a
    # stale/orphaned row on collision (most recent OAuth consent wins).
    crud_instagram.upsert_account(db, user.id, ig_user_id, long_lived_token, username)

    try:
        # Messages-only: this token was granted without the comments scope.
        await instagram_oauth.subscribe_to_webhooks(long_lived_token, fields="messages")
    except instagram_oauth.InstagramOAuthError as e:
        print(f"Failed to subscribe {ig_user_id} to Instagram webhooks on login: {e}")

    login_code = security.create_access_token(
        user.id, expires_delta=timedelta(minutes=INSTAGRAM_LOGIN_CODE_EXPIRE_MINUTES)
    )
    return RedirectResponse(f"{login_url}?ig_code={login_code}")


@router.post("/instagram/exchange", response_model=Token)
def exchange_instagram_login_code(payload: InstagramExchangeRequest, db: Session = Depends(get_db)):
    """Trades the short-lived code from /instagram/callback for a real session token."""
    # fail_open=False: like OTP verify, a guessing guard that must deny
    # rather than unlimit during a Redis outage.
    if not check_and_increment(
        f"ig_login_exchange:{payload.code[-12:]}", limit=5, window_seconds=300,
        fail_open=False,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please start Instagram login again.",
        )

    try:
        decoded = jwt.decode(payload.code, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = uuid.UUID(decoded["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired login code")

    user = crud_user.get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _mint_session_tokens(db, user)


@router.post("/impersonate", response_model=Token)
def impersonate_merchant(
    payload: ImpersonateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Support access (#75): an allow-listed operator mints a short-lived
    token acting as a merchant. forbid_impersonated blocks nesting (an
    impersonated token can never mint); the operator check below uses
    verified identities only (see support_access).
    """
    operator_label = support_access.operator_match(db, current_user)
    if operator_label is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support access only.",
        )
    if not check_and_increment(
        f"impersonate:{current_user.id}", limit=20, window_seconds=3600
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many impersonation attempts. Try again later.",
        )

    target = None
    if payload.target_user_id is not None:
        target = crud_user.get_user(db, payload.target_user_id)
    else:
        digits = normalize_phone((payload.target_phone or "").strip())
        if digits:
            target = crud_user.get_user_by_username(db, digits)
        if target is None and digits:
            target = crud_user.get_user_by_phone_number(db, digits)
    if target is None:
        raise HTTPException(status_code=404, detail="Merchant not found")
    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are already yourself.",
        )
    if support_access.is_support_operator(db, target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot impersonate another operator.",
        )

    token = support_access.create_impersonation_token(target.id, current_user.id)
    support_access.record_support_event(
        db, current_user.id, target.id, SupportAccessKind.START,
        operator_label=operator_label,
        target_label=support_access.target_label(target),
    )
    return {"access_token": token, "token_type": "bearer"}


@router.post("/impersonate/exit")
def exit_impersonation(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    impersonation=Depends(get_impersonation),
):
    """
    Ends an impersonated session audit-side. Stateless JWTs can't be
    revoked — the 30-minute expiry bounds the token — so this records END
    and the dashboard drops the token and restores the operator session.
    """
    if impersonation is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not an impersonated session.",
        )
    operator = crud_user.get_user(db, impersonation.operator_id)
    support_access.record_support_event(
        db, impersonation.operator_id, current_user.id, SupportAccessKind.END,
        operator_label=(
            support_access.operator_match(db, operator) if operator else None
        ),
        target_label=support_access.target_label(current_user),
    )
    return {"success": True}


@router.post("/trial", response_model=TrialRead)
def grant_trial(
    payload: TrialGrantRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Operator-only free trial (demo access): sets the merchant's
    trial_ends_at, which is_merchant_allowed_ai honors like a paid
    subscription until it passes. Same grant call re-grants (change the
    window anytime); sync runs immediately so Amara flips on in this
    request instead of at the next trigger.
    """
    from app.services import subscription_access

    operator_label = support_access.operator_match(db, current_user)
    if operator_label is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support access only.",
        )
    if not check_and_increment(
        f"trial:{current_user.id}", limit=30, window_seconds=3600
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many trial changes. Try again later.",
        )
    target = crud_user.get_user(db, payload.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    now = datetime.utcnow()
    if payload.days is not None:
        trial_ends_at = now + timedelta(days=payload.days)
    else:
        trial_ends_at = payload.ends_at
        if trial_ends_at.tzinfo is not None:
            trial_ends_at = trial_ends_at.replace(tzinfo=None)
        if trial_ends_at <= now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ends_at must be in the future (revoke instead to end a trial now).",
            )
    target.trial_ends_at = trial_ends_at
    db.commit()
    subscription_access.sync_ai_access_for_merchant(db, target)
    support_access.record_support_event(
        db, current_user.id, target.id, SupportAccessKind.TRIAL_GRANTED,
        operator_label=operator_label,
        target_label=support_access.target_label(target),
        detail=f"trial until {trial_ends_at.isoformat(timespec='minutes')}Z",
    )
    return TrialRead(
        user_id=target.id,
        trial_ends_at=trial_ends_at,
        amara_active=subscription_access.is_merchant_allowed_ai(db, target),
    )


@router.delete("/trial/{user_id}", response_model=TrialRead)
def revoke_trial(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Operator-only trial revoke: clears the window immediately (Amara flips
    off in this request unless a paid subscription holds it on).
    Idempotent — revoking a merchant with no trial still 200s.
    """
    from app.services import subscription_access

    operator_label = support_access.operator_match(db, current_user)
    if operator_label is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support access only.",
        )
    if not check_and_increment(
        f"trial:{current_user.id}", limit=30, window_seconds=3600
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many trial changes. Try again later.",
        )
    target = crud_user.get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    had_trial = target.trial_ends_at is not None
    target.trial_ends_at = None
    db.commit()
    subscription_access.sync_ai_access_for_merchant(db, target)
    support_access.record_support_event(
        db, current_user.id, target.id, SupportAccessKind.TRIAL_REVOKED,
        operator_label=operator_label,
        target_label=support_access.target_label(target),
        detail="trial revoked" if had_trial else "revoke with no trial set (no-op)",
    )
    return TrialRead(
        user_id=target.id,
        trial_ends_at=None,
        amara_active=subscription_access.is_merchant_allowed_ai(db, target),
    )


@router.get("/session", response_model=SessionRead)
def get_session(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    impersonation=Depends(get_impersonation),
):
    """Who this token acts as, operator status, and who's behind the mask."""
    if impersonation is not None:
        operator = crud_user.get_user(db, impersonation.operator_id)
        return SessionRead(
            user=current_user,
            is_operator=True,
            impersonated_by=ImpersonatorRef(
                id=impersonation.operator_id,
                label=(
                    support_access.operator_match(db, operator)
                    if operator else None
                ),
            ),
        )
    return SessionRead(
        user=current_user,
        is_operator=support_access.is_support_operator(db, current_user),
        impersonated_by=None,
    )


@router.get("/merchants", response_model=MerchantListRead)
def list_merchants(
    q: Optional[str] = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Support access (#75): operator-only merchant list backing the /admin UI.
    Returns exactly the merchants this operator can impersonate — operators
    and self are excluded (minting for either is a 400 anyway). Reads are
    not audit-logged (same rule as the WRITE-only middleware); the START
    row on impersonate bounds the window.
    """
    if support_access.operator_match(db, current_user) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support access only.",
        )
    candidates = crud_user.search_users(db, q)
    impersonable = [
        user for user in candidates
        if user.id != current_user.id
        and not support_access.is_support_operator(db, user)
    ]
    return MerchantListRead(
        items=impersonable[offset:offset + limit],
        total=len(impersonable),
    )


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
    phone_number is the self-reported billing contact for a merchant whose
    account has no phone tied to it (no username), matched against the
    Customer who paid for the platform subscription. See
    app/services/subscription_access.py. handoff_phone_number is the
    explicit handoff-alert override (empty clears it back to the OTP
    number); unlike phone_number it needs no uniqueness — two merchants
    may share an ops phone.
    """
    if payload.phone_number is not None:
        normalized = normalize_phone(payload.phone_number)
        existing = crud_user.get_user_by_phone_number(db, normalized)
        if existing and existing.id != current_user.id:
            raise HTTPException(status_code=400, detail="This phone number is already in use.")
        crud_user.set_user_phone_number(db, current_user, normalized)

    if payload.handoff_phone_number is not None:
        raw = payload.handoff_phone_number.strip()
        if not raw:
            crud_user.set_user_handoff_number(db, current_user, None)
        else:
            normalized = normalize_phone(raw)
            if not normalized or len(normalized) < 9:
                raise HTTPException(status_code=400, detail="That doesn't look like a phone number.")
            crud_user.set_user_handoff_number(db, current_user, normalized)

    return current_user


@router.get("/me/subscription", response_model=MySubscriptionRead)
def get_my_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Self-service status view — see app/services/self_service_billing.py."""
    from app.services import subscription_access
    from app.crud import user as crud_user

    amara_active = subscription_access.is_merchant_allowed_ai(db, current_user)
    has_handoff_number = crud_user.resolve_handoff_number(current_user) is not None
    subscription = self_service_billing.get_my_subscription(db, current_user)
    if not subscription:
        return MySubscriptionRead(amara_active=amara_active, has_handoff_number=has_handoff_number)

    return MySubscriptionRead(
        status=subscription.status.value,
        monthly_amount=subscription.monthly_amount,
        next_billing_at=subscription.next_billing_at,
        retry_count=subscription.retry_count,
        past_due_since=subscription.past_due_since,
        amara_active=amara_active,
        has_handoff_number=has_handoff_number,
    )


@router.post("/me/subscription/pay", response_model=PayNowResponse)
async def pay_now(
    payload: PayNowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _own_session: None = Depends(forbid_impersonated),
):
    """
    Dashboard-native Pay Now for Taskflow's own subscription — same flow
    regardless of whether this is a first payment, catching up a lapsed
    one, or resubscribing after cancellation. See
    app/services/self_service_billing.py.
    """
    try:
        result = await self_service_billing.pay_now(
            db, current_user, payload.phone_number, payload.product_id
        )
    except self_service_billing.NoPlatformProductConfiguredError:
        raise HTTPException(status_code=503, detail="Subscription billing isn't configured yet.")
    except self_service_billing.UnknownPlanError:
        raise HTTPException(status_code=400, detail="That isn't a platform plan.")
    except self_service_billing.PlanChoiceRequiredError:
        raise HTTPException(status_code=400, detail="Choose a plan to pay for.")
    except self_service_billing.PhoneNumberRequiredError:
        raise HTTPException(status_code=400, detail="A phone number is required to pay.")
    except self_service_billing.PhoneNumberInUseError:
        raise HTTPException(status_code=400, detail="This phone number is already in use.")
    except self_service_billing.AlreadySubscribedError:
        raise HTTPException(status_code=409, detail="This plan is already active — no new payment needed.")
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to start payment. Please try again.")

    return result
