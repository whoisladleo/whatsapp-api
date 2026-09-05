import os
import httpx
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import uuid
from app.core.config import settings
from app.db.models import Order, Customer

class TunzaaService:
    def __init__(self, api_key: str = None, secret_key: str = None, base_url: str = "https://pay.tunzaa.co.tz"):
        self.api_key = api_key or os.environ.get("TUNZAA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("TUNZAA_SECRET_KEY", "")
        self.base_url = base_url
        self._cached_token = None
        self._token_expiry = None

    def _environment_header(self) -> Dict[str, str]:
        # Required by the real Malipo/Tunzaa API on every authenticated
        # request; missing it either gets the request rejected or routed to
        # the wrong environment.
        return {"X-Environment": settings.TUNZAA_ENVIRONMENT}

    async def get_token(self) -> str:
        # Check cache
        if self._cached_token and self._token_expiry and datetime.utcnow() < self._token_expiry:
            return self._cached_token

        # Exchange API Key & Secret for short-lived token
        payload = {
            "api_key": self.api_key,
            "secret_key": self.secret_key
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/accounts/request/token",
                    json=payload,
                    headers=self._environment_header(),
                    timeout=10.0
                )
                response.raise_for_status()
                data = response.json()
                self._cached_token = data.get("access_token")
                # Real response includes expires_in (seconds); refresh 5
                # minutes early as a safety margin. Fall back to a
                # conservative default only if the field is unexpectedly
                # missing.
                expires_in = data.get("expires_in", 3600)
                margin = min(300, max(0, expires_in - 1))
                self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - margin)
                return self._cached_token
            except Exception as e:
                print(f"Tunzaa token error: {e}")
                raise

    async def initiate_payment(self, phone: str, amount: float, reference: str) -> Dict[str, Any]:
        """
        Initiate a direct mobile money push.
        """
        payload = {
            "customer_msisdn": phone,
            "amount": str(int(amount)),
            "reference": reference
        }

        headers = {
            "Authorization": f"Bearer {await self.get_token()}",
            "Content-Type": "application/json",
            **self._environment_header(),
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/initiate-payment",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Tunzaa initiate_payment error: {e}")
            raise

    async def refund_payment(self, transaction_id: str, amount: float, reason: str) -> Dict[str, Any]:
        """
        Reverses a completed payment — used when a number purchase's payment
        succeeds but the actual Twilio/Meta provisioning then fails (see
        app/tasks/number_billing.py), so the merchant isn't charged for a
        number they never got.

        UNVERIFIED: unlike initiate_payment/create_installment_plan/
        get_installment_plan (each confirmed against Tunzaa's real
        sandbox API per the comments on those methods), no real Tunzaa
        refund endpoint has been exercised against sandbox/production yet —
        this endpoint path and payload shape are a best-effort guess
        mirroring initiate_payment's shape. Confirm against Tunzaa's actual
        API (or support) before relying on this in production; until then,
        a failure here is logged and swallowed by callers rather than
        trusted to have silently succeeded.
        """
        payload = {
            "transaction_id": transaction_id,
            "amount": str(int(amount)),
            "reason": reason,
        }
        headers = {
            "Authorization": f"Bearer {await self.get_token()}",
            "Content-Type": "application/json",
            **self._environment_header(),
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/refund",
                    json=payload,
                    headers=headers,
                    timeout=10.0,
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Tunzaa refund_payment error: {e}")
            raise

    async def create_installment_plan(self, order: Order, product_name: str) -> Dict[str, Any]:
        """
        Create a Tunzaa Lipa kwa Awamu installment plan.
        """
        start_date = datetime.utcnow().strftime("%Y-%m-%d")
        end_date = (datetime.utcnow() + timedelta(days=90)).strftime("%Y-%m-%d")
        
        payload = {
            "customer": {
                "first_name": "Valued",
                "last_name": "Customer",
                "phone": order.customer.phone_number,
                "address": order.customer.delivery_address or "Unknown"
            },
            "name": f"{product_name} - 3 Month Plan",
            "description": f"Installment payment for {product_name}",
            "total_amount": float(order.agreed_price),
            "payment_frequency": "monthly",
            "start_date": start_date,
            "end_date": end_date
        }
        
        headers = {
            "Authorization": f"Bearer {await self.get_token()}",
            "Content-Type": "application/json",
            **self._environment_header(),
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/installments/create",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Tunzaa create_installment error: {e}")
            raise

    async def get_installment_plan(self, plan_id: str, include_payments: bool = False) -> Dict[str, Any]:
        """
        Fetches a plan's authoritative state — total_amount/paid_amount/
        remaining_balance at the plan level, plus each installment's own
        status. This is the real source of truth for how much has actually
        been paid on a plan (Tunzaa tracks cumulative payment at the plan
        level, not just a binary paid/unpaid per installment), used to
        reconcile our local Installment/Order rows after a webhook callback
        rather than trusting a single callback's fields in isolation.

        Verified against the real sandbox API (GET /installments/{plan_id}
        ?include_payments=true) — the response includes plan/installments in
        the shape used below, plus payment_history/progress fields this
        method doesn't currently use.
        """
        headers = {
            "Authorization": f"Bearer {await self.get_token()}",
            **self._environment_header(),
        }
        params = {"include_payments": "true"} if include_payments else None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/installments/{plan_id}",
                    params=params,
                    headers=headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Tunzaa get_installment_plan error: {e}")
            raise

tunzaa_service = TunzaaService()
