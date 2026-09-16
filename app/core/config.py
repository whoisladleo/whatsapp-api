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

    VITE_API_URL: Optional[str] = None

    # Tunzaa
    TUNZAA_API_KEY: Optional[str] = None
    TUNZAA_SECRET_KEY: Optional[str] = None
    # Required by the real Malipo/Tunzaa API on every authenticated request
    # (X-Environment header) — defaults to "sandbox" so a misconfigured
    # deployment fails safe rather than accidentally hitting production.
    TUNZAA_ENVIRONMENT: str = "sandbox"

    # Template used for order-reminder nudges sent outside the 24h WhatsApp
    # customer-service window (see app/tasks/order_reminders.py). Defaults to
    # Meta's always-approved sample template; production use requires a real
    # template approved for this account in Meta Business Manager.
    WHATSAPP_REMINDER_TEMPLATE_NAME: str = "hello_world"

    # Authentication-category template for OTP login (Meta requires these
    # outside the 24h customer-service window, which is always the case for
    # a first-time login OTP). None disables template sending and falls back
    # straight to the plain-text OTP message; set this once the template is
    # approved in Meta Business Manager.
    WHATSAPP_OTP_TEMPLATE_NAME: Optional[str] = "taskflow_login"

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
        "https://whatsapp.whoisladleo.dev",
    ]

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


settings = Settings()
