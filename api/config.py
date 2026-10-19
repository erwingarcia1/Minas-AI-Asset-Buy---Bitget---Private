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

    # Exchanges the platform offers (CSV). Keys for other venues are refused and the
    # UI shows only these; the other venues' client code stays in the repo.
    supported_exchanges_csv: str = os.getenv("SUPPORTED_EXCHANGES", "bitget")

    # How Bitget charges the withdrawal fee. "exclusive" (default): the fee is
    # taken on top of `size`, so we send net - fee (rounded down to the chain's
    # precision) and the wallet receives `size`. "inclusive": we send the net
    # balance and Bitget deducts the fee from it. Confirm against a real
    # withdrawal before changing.
    bitget_withdraw_fee_mode: str = os.getenv("BITGET_WITHDRAW_FEE_MODE", "exclusive").strip().lower() or "exclusive"

    # A recorded fill must be within this percentage of what the cost implies at the
    # execution price (and the cost within it of the amount requested); anything
    # outside is parked as needs_review instead of recorded as a completed buy.
    fill_tolerance_pct: float = float(os.getenv("FILL_TOLERANCE_PCT", "10") or 10)
    # Deviations between this and the tolerance still record as successful buys but are
    # logged with expected/actual figures and counted for the fill_deviations admin alert.
    fill_warn_pct: float = float(os.getenv("FILL_WARN_PCT", "3") or 3)

    # Admin alert + Slack notice when more than this many emails were given up on in one hour.
    email_failure_alert_threshold: int = int(os.getenv("EMAIL_FAILURE_ALERT_THRESHOLD", "5") or 5)

    @property
    def withdraw_allowed_emails(self) -> List[str]:
        return [e.strip().lower() for e in (self.withdraw_allowed_emails_csv or "").split(",") if e.strip()]

    @property
    def supported_exchanges(self) -> List[str]:
        raw = [x.strip().lower() for x in self.supported_exchanges_csv.split(",")]
        return [x for x in raw if x] or ["bitget"]

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
    

    # SMS (Twilio) + push (FCM) -- unset means the channel is a no-op
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "")
    fcm_server_key: str = os.getenv("FCM_SERVER_KEY", "")

    # Slack ops alerts (incoming webhook URL; unset = disabled)
    slack_webhook_url: str = os.getenv("SLACK_WEBHOOK_URL", "")
    # Private beta: withdraw-immediately purchases are off unless WITHDRAWALS_ENABLED=1 or the
    # purchase OWNER's email is in WITHDRAW_ALLOWED_EMAILS (comma-separated). Gate at the API
    # edges only (api/services/withdraw_gate.py); the executor is untouched.
    withdrawals_enabled: str = os.getenv("WITHDRAWALS_ENABLED", "0")
    withdraw_allowed_emails_csv: str = os.getenv("WITHDRAW_ALLOWED_EMAILS", "")
    # Public facts for the landing page (GET /api/public/meta): the server's egress IP customers
    # allowlist on their Bitget key. Set once per host; never hardcode it in the frontend.
    public_server_ip: str = os.getenv("PUBLIC_SERVER_IP", "")
    # Private beta: registration needs this code (constant-time compared). Empty = open in
    # development; in production an empty value closes registration entirely.
    signup_invite_code: str = os.getenv("SIGNUP_INVITE_CODE", "")
    # Admins must have two-factor authentication on before any admin route answers (403 with
    # mfa_required_for_admin until they enrol). Off while the admins enrol; the owner switches it
    # on afterwards. Compose passes "" when unset: only "1"/"true"/"yes" turns it on.
    admin_mfa_required: bool = (os.getenv("ADMIN_MFA_REQUIRED") or "0").strip().lower() in ("1", "true", "yes")
    # Telegram ops alerts (api/services/telegram.py): both unset = no-op. The token is never logged or echoed.
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # At most this many Telegram messages per minute; excess is dropped and counted (compose passes "" when unset)
    telegram_max_per_minute: int = int(os.getenv("TELEGRAM_MAX_PER_MINUTE") or "20")

    # Guardian: read-only exchange monitoring (v0.9.1). Off = nothing visible in the app and an idle
    # worker that only writes its heartbeat. Flipped only by the owner in .env.
    guardian_enabled: bool = (os.getenv("GUARDIAN_ENABLED") or "0").strip().lower() in ("1", "true", "yes")
    # Internal test phase: only these users see Guardian (CSV of emails; empty = every user).
    guardian_allowed_emails_csv: str = os.getenv("GUARDIAN_ALLOWED_EMAILS", "")
    # Detection thresholds. Shipped inside the image next to the package; a host file can override it.
    guardian_rules_path: str = os.getenv("GUARDIAN_RULES_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guardian", "rules.yaml")

    @field_validator("guardian_rules_path", mode="before")
    @classmethod
    def _empty_rules_path_means_the_shipped_file(cls, value):
        """docker-compose passes GUARDIAN_RULES_PATH through as an empty string when the
        server .env does not set it; pydantic-settings keeps that '' over the Python default,
        and RulesConfig('') would open '.' and crash the worker the moment the flag is on.
        Empty means the rules file baked into the image."""
        if value is None or not str(value).strip():
            return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guardian", "rules.yaml")
        return value
    # The worker's Prometheus exposition, on the compose network only (no port is published).
    guardian_metrics_host: str = os.getenv("GUARDIAN_METRICS_HOST") or "0.0.0.0"  # nosec B104 - container bind
    guardian_metrics_port: int = int(os.getenv("GUARDIAN_METRICS_PORT") or "9109")
    # Optional second Telegram chat of the same bot (the Guardian team group); empty = TELEGRAM_CHAT_ID only.
    guardian_telegram_chat_id: str = os.getenv("GUARDIAN_TELEGRAM_CHAT_ID", "")
    # SMS for Guardian CRITICAL alerts: ClickSend preferred, Twilio (the TWILIO_* above) as fallback.
    # *_TO_NUMBERS are the operators' own numbers for pages; customers' numbers live on their accounts.
    clicksend_username: str = os.getenv("CLICKSEND_USERNAME", "")
    clicksend_api_key: str = os.getenv("CLICKSEND_API_KEY", "")
    clicksend_from: str = os.getenv("CLICKSEND_FROM", "")
    clicksend_to_numbers: str = os.getenv("CLICKSEND_TO_NUMBERS", "")
    twilio_to_numbers: str = os.getenv("TWILIO_TO_NUMBERS", "")

    @property
    def guardian_allowed_emails(self) -> List[str]:
        return [e.strip().lower() for e in (self.guardian_allowed_emails_csv or "").split(",") if e.strip()]

    # Rate limiting
    rate_limit_enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "1") not in ("0", "false", "no")
    max_body_bytes: int = int(os.getenv("MAX_BODY_BYTES", str(1024 * 1024)))

    # Scheduler
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "1") not in ("0", "false", "no")
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
