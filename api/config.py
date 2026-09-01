from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    api_env: str = os.getenv("API_ENV", "development")
    secret_key: str = os.getenv("SECRET_KEY", "your-secret-key-change-this")
    
    # Database
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://autobuy:autobuy_password@localhost:5432/autobuy_db"
    )
    
    # Email (Resend)
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    support_email: str = os.getenv("SUPPORT_EMAIL", "support@minastech.ca")
    
    # Binance
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    
    # Sumsub (KYC)
    sumsub_app_token: str = os.getenv("SUMSUB_APP_TOKEN", "")
    sumsub_secret_key: str = os.getenv("SUMSUB_SECRET_KEY", "")
    sumsub_webhook_secret: str = os.getenv("SUMSUB_WEBHOOK_SECRET", "")
    sumsub_level_name: str = os.getenv("SUMSUB_LEVEL_NAME", "basic-kyc-level")
    sumsub_api_url: str = os.getenv("SUMSUB_API_URL", "https://api.sumsub.com")
    
    # JWT
    algorithm: str = os.getenv("ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
    refresh_token_expire_days: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
    
    # CORS
    allowed_origins: List[str] = ["http://localhost:3000", "http://localhost:8000"]
    
    # Scheduler
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "1") not in ("0", "false", "no")
    scheduler_poll_seconds: int = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    # Live trading is opt-in: orders are simulated unless AUTOBUY_DRY_RUN=0.
    autobuy_dry_run: bool = os.getenv("AUTOBUY_DRY_RUN", "1") not in ("0", "false", "no")

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        env_file = ".env"

settings = Settings()
