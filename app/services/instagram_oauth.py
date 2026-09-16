from typing import Tuple
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class InstagramOAuthError(Exception):
    """Raised when a step of the Business Login for Instagram exchange fails."""


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.INSTAGRAM_APP_ID,
        "redirect_uri": settings.INSTAGRAM_REDIRECT_URI,
        "response_type": "code",
        "scope": (
            "instagram_business_basic,"
            "instagram_business_manage_comments,"
            "instagram_business_manage_messages"
        ),
        "state": state,
    }
    return f"{settings.INSTAGRAM_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_short_lived_token(code: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            settings.INSTAGRAM_OAUTH_TOKEN_URL,
            data={
                "client_id": settings.INSTAGRAM_APP_ID,
                "client_secret": settings.INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": settings.INSTAGRAM_REDIRECT_URI,
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
    token = data.get("access_token")
    if not token:
        raise InstagramOAuthError(f"Long-lived token exchange failed: {data}")
    return token


async def fetch_account_identity(access_token: str) -> Tuple[str, str]:
    """Returns (ig_user_id, username) for the connected Instagram account."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.INSTAGRAM_GRAPH_BASE_URL}/me",
            params={"fields": "id,username", "access_token": access_token},
        )
    data = response.json()
    ig_user_id = data.get("id")
    if not ig_user_id:
        raise InstagramOAuthError(f"Failed to fetch Instagram account identity: {data}")
    return ig_user_id, data.get("username")
