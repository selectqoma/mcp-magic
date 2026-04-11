from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/govcon"

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_async_driver(cls, v: str) -> str:
        """Railway provides postgresql:// — rewrite to postgresql+asyncpg:// for SQLAlchemy async."""
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # API Keys
    sam_gov_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Billing (Paddle)
    paddle_api_key: str = ""
    paddle_webhook_secret: str = ""
    # Exact Paddle price IDs — set these from the Paddle dashboard; used to map subscriptions to tiers
    paddle_price_id_pro: str = ""
    paddle_price_id_agency: str = ""

    # Email (Resend)
    resend_api_key: str = ""

    # App
    secret_key: str = "change-me-in-production"
    debug: bool = False

    # Sync intervals (seconds)
    sam_sync_interval: int = 900       # 15 min
    grants_sync_interval: int = 1800   # 30 min
    sbir_sync_interval: int = 3600     # 1 hr
    usaspending_sync_interval: int = 21600  # 6 hrs

    # Embedding model
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Haiku model for PDF parsing
    haiku_model: str = "claude-haiku-4-5-20251001"


settings = Settings()
