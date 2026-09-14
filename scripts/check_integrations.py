#!/usr/bin/env python3
"""Check that the Sumsub and Resend credentials in the environment work.

Read-only: one authenticated request per provider, nothing is created or
sent. Credentials come from the environment (or a .env file in the current
directory); they are never printed.

    python scripts/check_integrations.py

Sumsub authenticates every request with an app token plus an HMAC-SHA256
signature of "<unix ts><METHOD><path+query><body>" using the secret key,
sent as X-App-Token / X-App-Access-Ts / X-App-Access-Sig (the same scheme
as api/services/sumsub_client.py). A "Bearer" header is not accepted.
"""
import hashlib
import hmac
import os
import sys
import time

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # optional: the variables may already be exported
    pass

SUMSUB_API_URL = os.getenv("SUMSUB_API_URL", "https://api.sumsub.com").rstrip("/")
RESEND_API_URL = "https://api.resend.com"
TIMEOUT = 10


def sumsub_headers(app_token: str, secret_key: str, method: str, path: str, body: str = "") -> dict:
    """Signed Sumsub headers for `method path` with `body` (empty for GET)."""
    ts = str(int(time.time()))
    sig = hmac.new(secret_key.encode(), f"{ts}{method.upper()}{path}{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-App-Token": app_token, "X-App-Access-Ts": ts, "X-App-Access-Sig": sig, "Content-Type": "application/json"}


def check_sumsub() -> bool:
    token, secret = os.getenv("SUMSUB_APP_TOKEN", ""), os.getenv("SUMSUB_SECRET_KEY", "")
    if not token or not secret:
        print("[sumsub] SKIPPED: SUMSUB_APP_TOKEN / SUMSUB_SECRET_KEY not set")
        return False
    # A cheap authenticated read: the applicant lookup for an id that does not
    # exist answers 404 with a valid signature and 401 with a bad one.
    path = "/resources/applicants/000000000000000000000000/one"
    try:
        resp = httpx.get(f"{SUMSUB_API_URL}{path}", headers=sumsub_headers(token, secret, "GET", path), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        print(f"[sumsub] FAILED: unreachable ({exc.__class__.__name__})")
        return False
    if resp.status_code == 401:
        print("[sumsub] FAILED: signature rejected (401) — check the app token and secret key")
        return False
    if resp.status_code in (200, 404):
        print(f"[sumsub] OK: signed request accepted (status {resp.status_code})")
        return True
    print(f"[sumsub] WARNING: unexpected status {resp.status_code}: {resp.text[:120]}")
    return False


def check_resend() -> bool:
    key = os.getenv("RESEND_API_KEY", "")
    if not key or key.startswith("re_placeholder"):
        print("[resend] SKIPPED: RESEND_API_KEY not set")
        return False
    try:
        resp = httpx.get(f"{RESEND_API_URL}/domains", headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        print(f"[resend] FAILED: unreachable ({exc.__class__.__name__})")
        return False
    if resp.status_code == 200:
        names = [d.get("name") for d in (resp.json().get("data") or [])]
        verified = [d.get("name") for d in (resp.json().get("data") or []) if d.get("status") == "verified"]
        print(f"[resend] OK: key valid; domains {names or 'none'}; verified {verified or 'none'}")
        sender = os.getenv("RESEND_FROM_EMAIL", "")
        domain = sender.split("@")[-1].rstrip(">").strip() if "@" in sender else ""
        if domain and domain not in verified:
            print(f"[resend] WARNING: RESEND_FROM_EMAIL domain {domain!r} is not among the verified domains")
        return True
    if resp.status_code in (401, 403):
        print(f"[resend] FAILED: key rejected (status {resp.status_code})")
        return False
    print(f"[resend] WARNING: unexpected status {resp.status_code}: {resp.text[:120]}")
    return False


if __name__ == "__main__":
    ok = [check_sumsub(), check_resend()]
    sys.exit(0 if all(ok) else 1)
