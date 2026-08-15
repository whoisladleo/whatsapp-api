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

    # LLM
    GOOGLE_API_KEY: Optional[str] = None

    VITE_API_URL: Optional[str] = None

    # Tunzaa
    TUNZAA_API_KEY: Optional[str] = None
    TUNZAA_SECRET_KEY: Optional[str] = None

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
