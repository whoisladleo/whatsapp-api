from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "WhatsApp Medium"
    API_V1_STR: str = "/api/v1"

    # Security
    SECRET_KEY: str = "your-super-secret-key-change-it"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Postgres
    POSTGRES_SERVER: str = "db"
    POSTGRES_PORT: str = "5432"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "whatsapp_db"
    SQLALCHEMY_DATABASE_URI: Optional[str] = None

    @property
    def database_url(self) -> str:
        if self.SQLALCHEMY_DATABASE_URI:
            return self.SQLALCHEMY_DATABASE_URI
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # WhatsApp Default (from env)
    WHATSAPP_TOKEN: Optional[str] = None
    PHONE_NUMBER_ID: Optional[str] = None
    WHATSAPP_VERSION: str = "v25.0"
    WHATSAPP_BASE_URL: str = "https://graph.facebook.com"
    WEBHOOK_VERIFY_TOKEN: Optional[str] = None
    # Meta App Secret, used to verify the X-Hub-Signature-256 header on
    # incoming webhook POSTs. If unset, signature verification is skipped
    # (with a warning) rather than rejecting all webhook traffic.
    WHATSAPP_APP_SECRET: Optional[str] = None

    # Instagram (Instagram API with Instagram Login / "Business Login for
    # Instagram") — a separate Meta App from the WhatsApp one above, so it
    # gets its own app id/secret/verify token/API host rather than reusing
    # the WHATSAPP_* settings. No Facebook Page is involved in this flow.
    INSTAGRAM_APP_ID: Optional[str] = None
    INSTAGRAM_APP_SECRET: Optional[str] = None
    INSTAGRAM_WEBHOOK_VERIFY_TOKEN: Optional[str] = None
    INSTAGRAM_GRAPH_BASE_URL: str = "https://graph.instagram.com"
    INSTAGRAM_OAUTH_AUTHORIZE_URL: str = "https://www.instagram.com/oauth/authorize"
    INSTAGRAM_OAUTH_TOKEN_URL: str = "https://api.instagram.com/oauth/access_token"
    INSTAGRAM_REDIRECT_URI: Optional[str] = None
    # Where to send the merchant's browser back to once the OAuth callback
    # finishes (success or failure) — the dashboard's settings page.
    DASHBOARD_URL: str = "https://business.taskflowos.dev"

    # The Product.slug of Taskflow's own "Taskflow OS Subscription" product
    # — app/services/subscription_access.py checks a merchant's payment
    # status against this specific product to decide whether their Amara
    # stays enabled. Deliberately explicit config rather than inferred by
    # name-matching (e.g. account.name == "taskflow"): getting this wrong
    # would either lock out every merchant or gate nobody at all. Gating is
    # a no-op (nobody affected) while this is unset.
    PLATFORM_SUBSCRIPTION_PRODUCT_SLUG: Optional[str] = None

    # Shared platform-level access token used for numbers auto-assigned from
    # the phone number pool (see PhoneNumberPool) — these all sit under
    # Taskflow's own Meta Business Manager, so one System User token covers
    # every pooled phone_number_id. Businesses that bring their own number
    # (BYON) still supply their own access_token at account-creation time.
    WHATSAPP_POOL_ACCESS_TOKEN: Optional[str] = None
    # The WhatsApp Business Account (WABA) ID that pool numbers live under —
    # needed to add/register new pool numbers via the Graph API
    # (POST /{WABA_ID}/phone_numbers etc.) rather than doing that setup by
    # hand in Meta Business Manager. Distinct from any individual
    # phone_number_id: a WABA can hold many numbers.
    WHATSAPP_POOL_WABA_ID: Optional[str] = None

    # Twilio — sources the actual phone numbers for automated pool
    # provisioning (see app/services/pool_provisioning.py). Distinct from
    # WHATSAPP_POOL_ACCESS_TOKEN/WABA_ID above: Twilio owns the number
    # itself and its inbound SMS (used to receive Meta's verification code),
    # Meta owns registering that number onto our WABA for messaging.
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    # Country to purchase pool numbers in. Twilio's local-number inventory
    # and regulatory-bundle requirements vary a lot by country — this
    # defaults to "US" because it's what we provision test numbers in today;
    # switching to Tanzania (or wherever pool numbers should ultimately be
    # local to) needs that regulatory question settled first, not just this
    # value changed.
    TWILIO_COUNTRY_CODE: str = "US"
    # This service's own publicly reachable base URL, needed at Twilio
    # number-purchase time to configure that number's inbound-SMS webhook
    # (POST {PUBLIC_BASE_URL}/api/v1/pool/twilio-sms-webhook) — Twilio has to
    # reach us, not the other way around, so localhost is never valid here.
    PUBLIC_BASE_URL: Optional[str] = None

    # LLM
    ANTHROPIC_API_KEY: Optional[str] = None
    # Which model backend LLMService uses: "claude" (Anthropic SDK) or
    # "muse" (OpenAI SDK pointed at Meta's API). Default stays claude.
    LLM_PROVIDER: str = "claude"
    MODEL_API_KEY: Optional[str] = None

    VITE_API_URL: Optional[str] = None

    # Tunzaa
    TUNZAA_API_KEY: Optional[str] = None
    TUNZAA_SECRET_KEY: Optional[str] = None
    # Required by the real Malipo/Tunzaa API on every authenticated request
    # (X-Environment header) — defaults to "sandbox" so a misconfigured
    # deployment fails safe rather than accidentally hitting production.
    TUNZAA_ENVIRONMENT: str = "sandbox"

    # Template used for order-reminder nudges sent outside the 24h WhatsApp
    # customer-service window (see app/tasks/order_reminders.py).
    WHATSAPP_REMINDER_TEMPLATE_NAME: str = "order_reminder"

    # Registered locale code, not just the Manager UI's display name — see
    # WHATSAPP_OTP_TEMPLATE_LANGUAGE's note on why "English"/"Swahili" in
    # the UI doesn't reliably mean "en_US"/"sw". "en"/"sw" confirmed as the
    # standing convention for every template on this account going forward.
    WHATSAPP_REMINDER_TEMPLATE_LANGUAGE: str = "en"

    # Authentication-category template for OTP login (Meta requires these
    # outside the 24h customer-service window, which is always the case for
    # a first-time login OTP). None disables template sending and falls back
    # straight to the plain-text OTP message; set this once the template is
    # approved in Meta Business Manager.
    WHATSAPP_OTP_TEMPLATE_NAME: Optional[str] = "taskflow_login"

    # The template's registered locale code — NOT necessarily "en_US" just
    # because Meta's UI labels it "English": templates created via the
    # WhatsApp Manager UI commonly register under the bare "en" code, and
    # sending with the wrong one fails with Meta error 132001 ("template
    # name does not exist in <language>"). Check the template's actual
    # code in WhatsApp Manager (not just its display name) before changing
    # this — confirmed "en" for taskflow_login as of 2026-09.
    WHATSAPP_OTP_TEMPLATE_LANGUAGE: str = "en"

    # Utility-category template for handoff alerts to a merchant's personal
    # WhatsApp (see LLMService._send_platform_alert_to_merchant). Required,
    # not optional like the OTP one: a merchant essentially never has an
    # open 24h session with the platform's own number (they only ever
    # *receive* from it), so a free-text alert gets accepted by the API
    # (200 OK) and then silently dropped as an out-of-window re-engagement
    # message — a real production incident where alerts never reached the
    # merchant's phone despite every send looking successful in our logs.
    WHATSAPP_HANDOFF_ALERT_TEMPLATE_NAME: str = "handoff_notification"

    # Merchant-facing, not customer-facing — there's no per-merchant
    # language field to key off (same limitation as
    # WHATSAPP_OTP_TEMPLATE_LANGUAGE), so this is one global default.
    # "en" confirmed as the registered code for handoff_notification as of
    # 2026-09. The template has a header ("A customer {{customer}} needs
    # your help right now.") and a body ("They said: \"{{message}}\" /
    # Reply directly using: /reply {{customer}} \"your message\"") — note
    # {{customer}} appears in BOTH, so a send must supply it in both the
    # header and body components' parameters, not just once. See
    # _send_platform_alert_to_merchant.
    WHATSAPP_HANDOFF_ALERT_TEMPLATE_LANGUAGE: str = "en"

    # Utility-category template for a merchant payment alert (any successful
    # payment — full, an installment leg, or whatever activates/completes a
    # subscription), sent the same way as the handoff alert above (platform
    # credentials, not the merchant's own account — same 24h-session
    # limitation applies). Header has a {{product}} variable; body has
    # {{summary}} and {{reference}} on their own template lines (newlines
    # inside a single variable's value don't render as line breaks — found
    # live while designing this). "en" confirmed as the registered code for
    # payment_received as of 2026-10. See _notify_merchant_of_payment.
    WHATSAPP_PAYMENT_ALERT_TEMPLATE_NAME: str = "payment_received"
    WHATSAPP_PAYMENT_ALERT_TEMPLATE_LANGUAGE: str = "en"

    # Monthly Amara reply allowance per merchant (#41). Warn-only: alerts at
    # 80% and 100% of this, Amara never stops replying. One global value
    # for now; a per-plan limit (Amara Pro) would need a per-merchant one.
    PLAN_REPLY_LIMIT: int = 2000
    # Utility template for the usage alert, named variables {{account}},
    # {{percent}}, {{total}}. Sent with platform credentials like the alerts
    # above, to the merchant and to OPS_ALERT_PHONE (a human's personal
    # WhatsApp — never the platform's own number, which can't message itself).
    WHATSAPP_USAGE_ALERT_TEMPLATE_NAME: str = "usage_warning"
    WHATSAPP_USAGE_ALERT_TEMPLATE_LANGUAGE: str = "en"
    OPS_ALERT_PHONE: Optional[str] = None

    # Cloudflare R2
    R2_ACCOUNT_ID: Optional[str] = None
    R2_ACCESS_KEY_ID: Optional[str] = None
    R2_SECRET_ACCESS_KEY: Optional[str] = None
    R2_BUCKET_NAME: Optional[str] = None
    R2_PUBLIC_URL: Optional[str] = None


    # Scaling
    WORKERS_PER_CORE: int = 1
    MAX_WORKERS: int = 4
    TIMEOUT: int = 120

    PORT: int = 8000
    BACKEND_CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://business.taskflowos.dev",
        "https://whatsapp.whoisladleo.dev",
    ]

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
