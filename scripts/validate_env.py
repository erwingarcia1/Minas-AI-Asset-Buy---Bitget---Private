#!/usr/bin/env python3
"""Validate a production .env before deploying the platform stack.

    python scripts/validate_env.py [.env]          # default: ./.env

Exit 0 when every required variable is present, non-placeholder and strong
enough for the API's production boot guard; exit 1 with one line per problem
otherwise. Values are never printed. Reads the file directly (no dotenv
dependency) so it works on a bare server.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED = ("API_ENV", "SECRET_KEY", "ENCRYPTION_KEY", "POSTGRES_PASSWORD",
            "DATABASE_URL", "REDIS_URL", "ALLOWED_ORIGINS",
            "RESEND_API_KEY", "RESEND_FROM_EMAIL", "APP_BASE_URL")
RECOMMENDED = ("SUPPORT_EMAIL", "RESEND_REPLY_TO", "APP_NAME", "SUMSUB_APP_TOKEN", "SUMSUB_SECRET_KEY",
               "SUMSUB_WEBHOOK_SECRET", "SUMSUB_LEVEL_NAME")
# Sender domains that can never deliver to customers.
BLOCKED_SENDER_DOMAINS = ("resend.dev", "autobuy.platform")
PLACEHOLDER = re.compile(r"CHANGE_ME|your[-_]|change[-_]this|example|placeholder|xxx", re.I)
WEAK_SECRETS = {"", "your-secret-key-change-this", "your-secret-key-change-this-in-production",
                "autobuy_password"}


def parse(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def check(env: dict[str, str]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []

    def placeholder(key: str) -> bool:
        return bool(PLACEHOLDER.search(env.get(key, "")))

    for key in REQUIRED:
        if not env.get(key):
            errors.append(f"{key} is missing or empty")
        elif placeholder(key):
            errors.append(f"{key} still holds a placeholder value")

    if env.get("API_ENV") and env["API_ENV"] != "production":
        errors.append(f"API_ENV must be 'production' (is '{env['API_ENV']}')")
    for key in ("SECRET_KEY", "ENCRYPTION_KEY"):
        val = env.get(key, "")
        if val in WEAK_SECRETS:
            errors.append(f"{key} is a known default")
        elif val and len(val) < 32:
            errors.append(f"{key} is shorter than 32 characters (boot guard refuses it)")
    if env.get("SECRET_KEY") and env.get("SECRET_KEY") == env.get("ENCRYPTION_KEY"):
        errors.append("SECRET_KEY and ENCRYPTION_KEY must differ (they rotate independently)")

    pw = env.get("POSTGRES_PASSWORD", "")
    if pw in WEAK_SECRETS:
        errors.append("POSTGRES_PASSWORD is the compose default")
    if pw and env.get("DATABASE_URL") and pw not in env["DATABASE_URL"]:
        warnings.append("DATABASE_URL does not contain POSTGRES_PASSWORD — host-side tools will fail to connect")
    if env.get("DATABASE_URL") and not env["DATABASE_URL"].startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must start with postgresql+asyncpg://")

    origins = [o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if any(o.startswith("http://") for o in origins):
        warnings.append("ALLOWED_ORIGINS contains a plain-http origin")
    if any("localhost" in o for o in origins):
        warnings.append("ALLOWED_ORIGINS contains localhost")

    # Email + links: the API's production boot guard refuses these too; fail early here.
    sender = env.get("RESEND_FROM_EMAIL", "").strip().lower()
    for bad in BLOCKED_SENDER_DOMAINS:
        if bad in sender:
            errors.append(f"RESEND_FROM_EMAIL uses {bad}, which cannot deliver to customers; use a domain verified in Resend")
    url = env.get("APP_BASE_URL", "").strip()
    if url and not url.lower().startswith("https://"):
        errors.append(f"APP_BASE_URL must start with https:// (is {url!r})")
    if url and ("localhost" in url.lower() or "127.0.0.1" in url):
        errors.append(f"APP_BASE_URL points at localhost/127.0.0.1 ({url!r}); emailed links would not reach the app")
    if env.get("LOG_RESET_LINKS", "").lower() in ("1", "true", "yes"):
        errors.append("LOG_RESET_LINKS=1 writes password reset links to the log; it must be unset or 0 in production")
    if not env.get("SUPPORT_EMAIL") and not env.get("RESEND_REPLY_TO"):
        warnings.append("neither RESEND_REPLY_TO nor SUPPORT_EMAIL is set — customer replies to emails go nowhere")

    if env.get("AUTOBUY_DRY_RUN", "1") in ("0", "false", "no"):
        warnings.append("AUTOBUY_DRY_RUN=0 — REAL ORDERS will execute")
    if env.get("RATE_LIMIT_ENABLED", "1") in ("0", "false", "no"):
        warnings.append("RATE_LIMIT_ENABLED=0 — the API is unthrottled")

    for key in RECOMMENDED:
        if not env.get(key):
            warnings.append(f"{key} is unset — the matching feature stays disabled")
        elif placeholder(key):
            warnings.append(f"{key} still holds a placeholder — the matching feature stays disabled")
    return errors, warnings


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else ".env")
    if not path.is_file():
        print(f"ERROR: {path} not found")
        return 1
    errors, warnings = check(parse(path))
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        print(f"\n{path}: {len(errors)} error(s) — do not deploy.")
        return 1
    print(f"\n{path}: OK ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
