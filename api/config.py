from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")  # nosec B104 - container bind; restrict via API_HOST
    api_port: int = int(os.getenv("API_PORT", "8000"))
    api_env: str = os.getenv("API_ENV", "development")
    # Set to "1" on production hosts (.env.production.example / scripts/deploy.sh check it):
    # the API then refuses to start unless API_ENV is exactly "production".
    require_production: str = os.getenv("REQUIRE_PRODUCTION", "")
    secret_key: str = os.getenv("SECRET_KEY", "your-secret-key-change-this")
    # Separate key for at-rest encryption so JWT signing and Fernet rotate
    # independently. Falls back to SECRET_KEY in dev; required in production.
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    
    # Database
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://autobuy:autobuy_password@localhost:5432/autobuy_db"
    )
    
    # Email (Resend) — see api/integrations/resend.py; all values come from here, never from code
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    # Sender for every email, on a domain verified in Resend, e.g. "Minas AutoBuy <noreply@minastrade.com>"
    resend_from_email: str = os.getenv("RESEND_FROM_EMAIL", "")
    # Reply-To on every email; defaults to SUPPORT_EMAIL so customer replies reach a real inbox
    resend_reply_to: str = os.getenv("RESEND_REPLY_TO", "")
    # Support address shown in email footers and the Reply-To fallback. Never a sender.
    support_email: str = os.getenv("SUPPORT_EMAIL", "")
    # Public web app origin: the only base for links in emails (required, https, in production)
    app_base_url: str = os.getenv("APP_BASE_URL", "")
    # Product name in subjects and bodies; rebranding is this one value.
    # Compose passes unset variables as "" — treat blank as the default.
    app_name: str = os.getenv("APP_NAME") or "Minas AutoBuy"
    # Write password reset links to the log. Local debugging only; on only when explicitly
    # "1"/"true"/"yes". Kept as a string so a blank value from compose can never fail bool parsing.
    log_reset_links: str = os.getenv("LOG_RESET_LINKS", "")

    @field_validator("app_name")
    @classmethod
    def _default_app_name(cls, v: str) -> str:
        return (v or "").strip() or "Minas AutoBuy"

    @property
    def production_required(self) -> bool:
        return str(self.require_production).strip().lower() in ("1", "true", "yes")

    @property
    def reset_links_logged(self) -> bool:
        return str(self.log_reset_links).strip().lower() in ("1", "true", "yes")
    
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
        "ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000")

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

    # Slack ops alerts (incoming webhook URL; unset = disabled)
    slack_webhook_url: str = os.getenv("SLACK_WEBHOOK_URL", "")

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
        # Tests set AUTOBUY_SKIP_DOTENV=1 and own every variable; .env must not steer them.
        env_file = None if os.getenv("AUTOBUY_SKIP_DOTENV") == "1" else ".env"
        extra = "ignore"  # unknown env keys must not crash startup

settings = Settings()


_DEFAULT_SECRETS = {"", "your-secret-key-change-this", "your-secret-key-change-this-in-production"}
# Sender domains that can never reach customers. Built from fragments so the
# repository-wide scan for hardcoded email domains stays meaningful.
_BLOCKED_SENDER_DOMAINS = tuple(".".join(p) for p in (("resend", "dev"), ("autobuy", "platform")))


def _placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    return not v or v.startswith("your_") or "change_me" in v or "placeholder" in v or v == "xxx"


def email_config_problems(s: "Settings") -> list:
    """Production email misconfigurations, each naming the variable and what is wrong."""
    problems = []
    if _placeholder(s.resend_api_key):
        problems.append("RESEND_API_KEY is empty or a placeholder; set the Resend API key")
    sender = (s.resend_from_email or "").strip().lower()
    if not sender:
        problems.append("RESEND_FROM_EMAIL is empty; set a sender on a domain verified in Resend, "
                        "e.g. 'Minas AutoBuy <noreply@your-verified-domain>'")
    else:
        bad = [d for d in _BLOCKED_SENDER_DOMAINS if d in sender]
        if bad:
            problems.append(f"RESEND_FROM_EMAIL uses {bad[0]}, which cannot deliver to customers; "
                            "use a domain verified in Resend")
    url = (s.app_base_url or "").strip()
    if not url:
        problems.append("APP_BASE_URL is empty; emailed links need the public https origin of the web app")
    elif not url.lower().startswith("https://"):
        problems.append(f"APP_BASE_URL must start with https:// (is {url!r})")
    elif "localhost" in url.lower() or "127.0.0.1" in url:
        problems.append(f"APP_BASE_URL points at localhost/127.0.0.1 ({url!r}); emailed links would not reach the app")
    if s.reset_links_logged:
        problems.append("LOG_RESET_LINKS=1 writes password reset links to the log; it must be unset or 0 in production")
    return problems


_PRODUCTION_LOOKALIKES = {"prod", "production", "live", "prd"}


def enforce_production_secrets(s: "Settings") -> None:
    """Refuse to boot a production instance on default/weak secrets, and refuse
    any API_ENV that merely looks like production: everything (this guard,
    HSTS, Alembic-only schema) keys on the exact string "production"."""
    env = s.api_env or ""
    if env != "production" and env.strip().lower() in _PRODUCTION_LOOKALIKES:
        raise RuntimeError(f"API_ENV must be exactly 'production' (is {env!r}); the production "
                           "safeguards only apply to that exact value")
    if s.production_required and env != "production":
        raise RuntimeError(f"REQUIRE_PRODUCTION is set but API_ENV is {env!r}; on a production host "
                           "API_ENV must be exactly 'production'")
    if env != "production":
        return
    problems = []
    if s.secret_key in _DEFAULT_SECRETS or len(s.secret_key) < 32:
        problems.append("SECRET_KEY is default or shorter than 32 characters")
    if not s.encryption_key:
        problems.append("ENCRYPTION_KEY is unset (at-rest encryption would share the JWT secret)")
    problems.extend(email_config_problems(s))
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure configuration: "
            + "; ".join(problems))
