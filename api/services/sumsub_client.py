"""Sumsub KYC REST client (applicants, status, WebSDK access tokens).

Requests are signed per Sumsub's scheme: HMAC-SHA256 over
"{ts}{METHOD}{path}{body}" with the secret key, sent alongside the app token.
"""
import hashlib
import hmac
import json
import time

import httpx

from api.config import settings


class SumsubError(Exception):
    pass


def map_review_to_kyc_status(review_status: str, review_answer: str | None) -> str:
    """Sumsub review state -> platform kyc_status (pending/verified/rejected)."""
    if review_status == "completed":
        if review_answer == "GREEN":
            return "verified"
        if review_answer == "RED":
            return "rejected"
    return "pending"


class SumsubClient:
    def __init__(self, app_token: str = "", secret_key: str = "", base_url: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.app_token = app_token or settings.sumsub_app_token
        self.secret_key = secret_key or settings.sumsub_secret_key
        self.base_url = (base_url or settings.sumsub_api_url).rstrip("/")
        self._transport = transport  # tests inject httpx.MockTransport

    @property
    def configured(self) -> bool:
        return bool(self.app_token and self.secret_key)

    def _headers(self, method: str, path: str, body: str) -> dict:
        ts = str(int(time.time()))
        sig = hmac.new(self.secret_key.encode(),
                       f"{ts}{method}{path}{body}".encode(),
                       hashlib.sha256).hexdigest()
        return {"X-App-Token": self.app_token, "X-App-Access-Ts": ts,
                "X-App-Access-Sig": sig, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload) if payload is not None else ""
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                resp = await client.request(
                    method, f"{self.base_url}{path}",
                    headers=self._headers(method, path, body),
                    content=body or None)
        except httpx.HTTPError as exc:
            raise SumsubError(f"Sumsub unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise SumsubError(f"Sumsub {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Signed request to an arbitrary Sumsub endpoint; returns the JSON body."""
        return await self._request(method, path, payload)

    async def create_applicant(self, external_user_id: str, email: str) -> str:
        """Create an applicant; returns the Sumsub applicant id."""
        data = await self._request(
            "POST", f"/resources/applicants?levelName={settings.sumsub_level_name}",
            {"externalUserId": external_user_id, "email": email})
        return data["id"]

    async def get_applicant_status(self, applicant_id: str) -> str:
        """Current review state mapped to pending/verified/rejected."""
        data = await self._request(
            "GET", f"/resources/applicants/{applicant_id}/status")
        return map_review_to_kyc_status(
            data.get("reviewStatus", ""),
            (data.get("reviewResult") or {}).get("reviewAnswer"))

    async def get_access_token(self, external_user_id: str) -> str:
        """WebSDK access token for the frontend widget (short-lived)."""
        data = await self._request(
            "POST",
            f"/resources/accessTokens?userId={external_user_id}"
            f"&levelName={settings.sumsub_level_name}")
        return data["token"]


def verify_webhook_signature(raw_body: bytes, digest_header: str,
                             secret: str = "") -> bool:
    """Sumsub webhooks carry x-payload-digest: HMAC-SHA256 hex of the raw body."""
    secret = secret or settings.sumsub_webhook_secret
    if not secret or not digest_header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest_header)
