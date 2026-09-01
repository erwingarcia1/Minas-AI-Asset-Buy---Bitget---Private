from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")  # nosec B104 - container bind; restrict via API_HOST
    api_port: int = int(os.getenv("API_PORT", "8000"))
    api_env: str = os.getenv("API_ENV", "development")
    secret_key: str = os.getenv("SECRET_KEY", "your-secret-key-change-this")
    # Separate key for at-rest encryption so JWT signing and Fernet rotate
    # independently. Falls back to SECRET_KEY in dev; required in production.
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    
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
    
    # Redis
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # CORS — CSV in the ALLOWED_ORIGINS env var
    allowed_origins_csv: str = os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000")

    @property
    def allowed_origins(self) -> List[str]:
        raw = self.allowed_origins_csv.strip()
        if raw.startswith("["):  # tolerate JSON-array form as well as CSV
            import json
            try:
                return [str(o) for o in json.loads(raw)]
            except ValueError:
                pass
        return [o.strip() for o in raw.split(",") if o.strip()]
    
    # Trading
    supported_cryptos: str = os.getenv(
        "SUPPORTED_CRYPTOS", "BTC,ETH,SOL,BNB,XRP,ADA,DOGE,LTC")

    # SMS (Twilio) + push (FCM) -- unset means the channel is a no-op
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "")
    fcm_server_key: str = os.getenv("FCM_SERVER_KEY", "")

    # Rate limiting
    rate_limit_enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "1") not in ("0", "false", "no")
    max_body_bytes: int = int(os.getenv("MAX_BODY_BYTES", str(1024 * 1024)))

    # Scheduler
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "1") not in ("0", "false", "no")
    scheduler_poll_seconds: int = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    # Live trading is opt-in: orders are simulated unless AUTOBUY_DRY_RUN=0.
    autobuy_dry_run: bool = os.getenv("AUTOBUY_DRY_RUN", "1") not in ("0", "false", "no")

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # unknown env keys must not crash startup

settings = Settings()


_DEFAULT_SECRETS = {"", "your-secret-key-change-this", "your-secret-key-change-this-in-production"}


def enforce_production_secrets(s: "Settings") -> None:
    """Refuse to boot a production instance on default/weak secrets."""
    if s.api_env != "production":
        return
    problems = []
    if s.secret_key in _DEFAULT_SECRETS or len(s.secret_key) < 32:
        problems.append("SECRET_KEY is default or shorter than 32 characters")
    if not s.encryption_key:
        problems.append("ENCRYPTION_KEY is unset (at-rest encryption would share the JWT secret)")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure configuration: "
            + "; ".join(problems))
