from typing import Tuple
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class FacebookOAuthError(Exception):
    """Raised when a step of the Facebook Login exchange fails."""


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": settings.FACEBOOK_LOGIN_REDIRECT_URI,
        "response_type": "code",
        # public_profile only — email isn't actually needed (the account is
        # keyed on facebook_id, not email) and requesting it 400s with
        # "Invalid Scopes: email" until it's added under the app's Use
        # Cases/Permissions page in the Meta dashboard. fetch_profile still
        # requests the email field and stores it opportunistically if a
        # future token happens to carry it.
        "scope": "public_profile",
        "state": state,
    }
    return f"{settings.FACEBOOK_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            settings.FACEBOOK_OAUTH_TOKEN_URL,
            params={
                "client_id": settings.FACEBOOK_APP_ID,
                "client_secret": settings.FACEBOOK_APP_SECRET,
                "redirect_uri": settings.FACEBOOK_LOGIN_REDIRECT_URI,
                "code": code,
            },
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise FacebookOAuthError(f"Token exchange failed: {data}")
    return token


async def fetch_profile(access_token: str) -> Tuple[str, str, str]:
    """Returns (facebook_id, name, email) for the logged-in Facebook user."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.FACEBOOK_GRAPH_BASE_URL}/me",
            params={"fields": "id,name,email", "access_token": access_token},
        )
    data = response.json()
    facebook_id = data.get("id")
    if not facebook_id:
        raise FacebookOAuthError(f"Failed to fetch Facebook profile: {data}")
    return facebook_id, data.get("name"), data.get("email")
