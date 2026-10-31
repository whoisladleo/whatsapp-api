from typing import Tuple
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class InstagramOAuthError(Exception):
    """Raised when a step of the Business Login for Instagram exchange fails."""


# Channel-connect scopes: same approved-only pair as LOGIN_SCOPES.
# manage_comments was stripped (#79) — Meta denied it, and requesting an
# unapproved scope fails the whole authorize for live merchants. Re-add it
# here (only) if Meta ever approves it.
CONNECT_SCOPES = "instagram_business_basic,instagram_business_manage_messages"


def _require_oauth_config(redirect_uri: str | None, setting_name: str) -> str:
    """
    Fails fast with a named setting instead of sending Meta a literal
    "None" client_id/redirect_uri and surfacing its cryptic
    Invalid redirect_uri error (#84 — observed live).
    """
    if not settings.INSTAGRAM_APP_ID:
        raise InstagramOAuthError("INSTAGRAM_APP_ID is not configured.")
    if not redirect_uri:
        raise InstagramOAuthError(f"{setting_name} is not configured.")
    return redirect_uri


def build_authorize_url(state: str) -> str:
    redirect_uri = _require_oauth_config(
        settings.INSTAGRAM_REDIRECT_URI, "INSTAGRAM_REDIRECT_URI"
    )
    params = {
        "client_id": settings.INSTAGRAM_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": CONNECT_SCOPES,
        "state": state,
    }
    return f"{settings.INSTAGRAM_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


# Login-time scopes: identity + DM automation only. manage_comments is
# deliberately excluded — Meta hasn't approved it, and requesting an
# unapproved scope makes the whole authorize request fail for merchants
# outside the app's test roles. The login flow auto-connects the DM
# channel (same as /oauth/callback minus comments), so this is all a
# new merchant needs on day one.
LOGIN_SCOPES = "instagram_business_basic,instagram_business_manage_messages"


def build_login_authorize_url(state: str) -> str:
    """Authorize URL for Instagram Login (/auth/instagram/start) — same app
    as the channel-connect flow, but its own redirect URI and scopes."""
    redirect_uri = _require_oauth_config(
        settings.INSTAGRAM_LOGIN_REDIRECT_URI, "INSTAGRAM_LOGIN_REDIRECT_URI"
    )
    params = {
        "client_id": settings.INSTAGRAM_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LOGIN_SCOPES,
        "state": state,
    }
    return f"{settings.INSTAGRAM_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_short_lived_token(code: str, redirect_uri: str | None = None) -> str:
    # Meta requires the token exchange to echo the exact redirect_uri the
    # authorize request used — the login flow passes its own.
    async with httpx.AsyncClient() as client:
        response = await client.post(
            settings.INSTAGRAM_OAUTH_TOKEN_URL,
            data={
                "client_id": settings.INSTAGRAM_APP_ID,
                "client_secret": settings.INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri or settings.INSTAGRAM_REDIRECT_URI,
                "code": code,
            },
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise InstagramOAuthError(f"Short-lived token exchange failed: {data}")
    return token


async def exchange_for_long_lived_token(short_lived_token: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.INSTAGRAM_APP_SECRET,
                "access_token": short_lived_token,
            },
        )
    data = response.json()
    print(f"Long-lived token exchange response: {data}")
    token = data.get("access_token")
    if not token:
        raise InstagramOAuthError(f"Long-lived token exchange failed: {data}")
    return token


async def refresh_long_lived_token(access_token: str) -> str:
    """Refreshes a long-lived token before its ~60-day expiry (#80)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": access_token,
            },
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise InstagramOAuthError(f"Token refresh failed: {data}")
    return token


async def fetch_account_identity(access_token: str) -> Tuple[str, str]:
    """
    Returns (ig_user_id, username) for the connected Instagram account.
    Confirmed live against Meta: /me's `id` field is an app-scoped id (only
    meaningful to this app), NOT the id that shows up in webhook payloads'
    entry[].id — that's the separate `user_id` field. Requesting the wrong
    one here silently breaks account lookup for every future webhook.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/me",
            params={"fields": "user_id,username", "access_token": access_token},
        )
    data = response.json()
    ig_user_id = data.get("user_id")
    if not ig_user_id:
        raise InstagramOAuthError(f"Failed to fetch Instagram account identity: {data}")
    return ig_user_id, data.get("username")


async def subscribe_to_webhooks(access_token: str, fields: str = "comments,messages") -> None:
    """
    The app-level webhook callback URL/verify token (configured once in the
    Meta dashboard) isn't enough on its own — each connected account has to
    separately opt itself in via this call, using its own access token, or
    Meta never sends comment events for it. Best-effort: logged by the
    caller rather than raised, since a failure here shouldn't block the
    OAuth flow itself completing. The login flow passes messages-only —
    its token was granted without the comments scope.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/me/subscribed_apps",
            params={"subscribed_fields": fields, "access_token": access_token},
        )
    data = response.json()
    if not data.get("success"):
        raise InstagramOAuthError(f"Webhook subscription failed: {data}")


async def unsubscribe_from_webhooks(access_token: str) -> None:
    """
    Counterpart to subscribe_to_webhooks, called when a merchant disconnects
    — tells Meta to stop sending events for this account before the local
    access token is cleared. Best-effort, same as subscribing: logged by the
    caller rather than raised, since a merchant disconnecting shouldn't be
    blocked by a Meta-side hiccup (the local token gets cleared regardless,
    so Amara stops acting on this account's behalf either way).
    """
    async with httpx.AsyncClient() as client:
        response = await client.delete(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/me/subscribed_apps",
            params={"access_token": access_token},
        )
    data = response.json()
    if not data.get("success"):
        raise InstagramOAuthError(f"Webhook unsubscribe failed: {data}")
