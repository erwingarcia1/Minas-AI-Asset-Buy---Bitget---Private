"""Authentication routes: register, login (with the optional second factor), verify, refresh, logout."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Response
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import MFA_PENDING_TTL, Token, create_access_token, hash_password, token_cutoff, verify_password, verify_token
from api.config import settings
from api.database import get_db
from api.models import User
from api.routes.user import UTCModel, get_current_user
from fastapi import Request
from fastapi.responses import JSONResponse
from api.services.rate_limit import limiter, is_locked_out, register_failed_login, clear_failed_logins, user_or_ip_key
from api.services import guardian_identity, twofactor

import hashlib
import json
import hmac
import asyncio
import logging
import secrets

from api.services.email_service import email_service
from api.utils.links import build_app_link

logger = logging.getLogger("autobuy.auth")

PASSWORD_RESET_TTL = timedelta(hours=1)
RESET_USED_PREFIX = "pwreset-used:"

router = APIRouter()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein123", "admin123", "welcome123", "iloveyou1", "sunshine1",
}


def validate_password_strength(value: str) -> str:
    problems = []
    if not any(c.islower() for c in value):
        problems.append("a lowercase letter")
    if not any(c.isupper() for c in value):
        problems.append("an uppercase letter")
    if not any(c.isdigit() for c in value):
        problems.append("a digit")
    if not any(not c.isalnum() for c in value):
        problems.append("a special character")
    if problems:
        raise ValueError("password must contain " + ", ".join(problems))
    # normalize away case and decoration so "Password123!" still matches
    base = "".join(c for c in value.lower() if c.isalnum())
    if value.lower() in COMMON_PASSWORDS or base in COMMON_PASSWORDS:
        raise ValueError("password is too common")
    return value


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    invite_code: Optional[str] = Field(default=None, max_length=128)   # private beta

    _strength = field_validator("password")(validate_password_strength)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    sent: bool = True


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20, max_length=2048)
    new_password: str = Field(min_length=8, max_length=128)

    _strength = field_validator("new_password")(validate_password_strength)


class ResetTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=2048)


class ResetTokenValidity(BaseModel):
    valid: bool


class UserOut(UTCModel):
    id: UUID
    email: EmailStr
    kyc_status: str
    created_at: datetime


class TokenResponse(BaseModel):
    """The access token is returned in the body and must be kept in memory only.
    The refresh token never appears in a body: it travels in an httpOnly cookie
    scoped to /api/auth, and `csrf_token` (also set as a readable cookie) must
    be echoed in the X-CSRF-Token header on /refresh and /logout."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    csrf_token: str


class VerifyRequest(BaseModel):
    token: str


class VerifyResponse(BaseModel):
    valid: bool
    user_id: Optional[str] = None


class MfaRequiredResponse(BaseModel):
    """The password was accepted but the account has two-factor authentication
    on. No session is issued and no cookie is set: exchange `mfa_token` plus a
    code at /login/mfa within `expires_in` seconds."""
    mfa_required: bool = True
    mfa_token: str
    expires_in: int


class MfaLoginRequest(BaseModel):
    mfa_token: str = Field(min_length=20, max_length=2048)
    code: Optional[str] = Field(default=None, max_length=32)
    recovery_code: Optional[str] = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _exactly_one_factor(self):
        if bool((self.code or "").strip()) == bool((self.recovery_code or "").strip()):
            raise ValueError("send either code or recovery_code")
        return self


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _create_refresh_token(user_id: str, prev_jti: Optional[str] = None) -> str:
    """Create a long-lived refresh token (type=refresh, revocable via jti).
    `prev_jti` names the token this one replaced, so logout and a password
    change can also drop the short grace entry that still hands this token out."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
    }
    if prev_jti:
        payload["prv"] = prev_jti
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


REVOKED_PREFIX = "revoked-refresh:"


async def _is_revoked(jti: str) -> bool:
    from api.services.redis_client import get_redis
    try:
        return bool(await get_redis().exists(REVOKED_PREFIX + jti))
    except Exception:
        logger.warning("revocation check unavailable (Redis down); allowing token")
        return False


async def _revoke(jti: str, exp_ts: int) -> None:
    """Hard revocation (logout): no grace follows."""
    from api.services.redis_client import get_redis
    import time as _time
    ttl = max(int(exp_ts - _time.time()), 1)
    await get_redis().setex(REVOKED_PREFIX + jti, ttl, "logout")


# --- rotation grace -----------------------------------------------------------
# Browser tabs share the refresh cookie. When two tabs refresh at the same moment
# exactly ONE request may rotate the token (an atomic SET NX claim on the revoked
# key); the other gets the very same new session back from a short-lived,
# encrypted cache keyed by the token it presented, so both tabs end up holding
# one identical cookie set and no second token chain is ever minted. The cache
# lives REFRESH_GRACE_SECONDS, covers only the immediately previous token, and
# is dropped by logout, by the next rotation and by a password change.
REFRESH_GRACE_SECONDS = 30
GRACE_PREFIX = "refresh-grace:"
CLAIM_WAIT_SECONDS = 2.0     # how long a losing request waits for the winner's cached session


async def _claim_rotation(jti: str, exp_ts: int) -> Optional[bool]:
    """True = this request rotates the token; False = someone already did
    (the value holds when); None = Redis unavailable (rotate without a claim)."""
    from api.services.redis_client import get_redis
    import time as _time
    ttl = max(int(exp_ts - _time.time()), 1)
    try:
        return bool(await get_redis().set(REVOKED_PREFIX + jti, repr(_time.time()), nx=True, ex=ttl))
    except Exception:
        logger.warning("rotation claim unavailable (Redis down); rotating without grace")
        return None


async def _store_grace(prev_jti: str, session: dict) -> None:
    """Cache the session minted for `prev_jti`, encrypted at rest, for the grace window. Never logged."""
    from api.routes.exchange import encrypt_value
    from api.services.redis_client import get_redis
    try:
        await get_redis().setex(GRACE_PREFIX + prev_jti, REFRESH_GRACE_SECONDS, encrypt_value(json.dumps(session)))
    except Exception:
        logger.warning("refresh grace entry not stored (Redis down)")


async def _drop_grace(prev_jti: Optional[str]) -> None:
    if not prev_jti:
        return
    from api.services.redis_client import get_redis
    try:
        await get_redis().delete(GRACE_PREFIX + prev_jti)
    except Exception:
        logger.warning("refresh grace entry not dropped (Redis down)")


async def _load_grace(prev_jti: str) -> Optional[dict]:
    from api.routes.exchange import decrypt_value
    from api.services.redis_client import get_redis
    try:
        raw = await get_redis().get(GRACE_PREFIX + prev_jti)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(decrypt_value(raw.decode() if isinstance(raw, bytes) else raw))
    except Exception:
        logger.warning("refresh grace entry unreadable; treated as absent")
        return None


async def _wait_for_grace(jti: str) -> Optional[dict]:
    """A request that lost the claim: if the winner claimed moments ago, wait for
    its cached session; a token revoked by logout or long ago gets nothing."""
    import asyncio
    import time as _time
    from api.services.redis_client import get_redis
    try:
        marker = await get_redis().get(REVOKED_PREFIX + jti)
    except Exception:
        return None
    marker = marker.decode() if isinstance(marker, bytes) else marker
    try:
        claimed_at = float(marker)
    except (TypeError, ValueError):
        return None          # "logout" (or a legacy "1"): no grace
    deadline = claimed_at + CLAIM_WAIT_SECONDS
    while True:
        cached = await _load_grace(jti)
        if cached is not None or _time.time() > deadline:
            return cached
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------
# Session cookies
# --------------------------------------------------------------------------
# The refresh token lives in an httpOnly cookie (JavaScript cannot read it, so
# an XSS cannot steal a long-lived credential); the CSRF token lives in a
# readable cookie and must be echoed in X-CSRF-Token on the endpoints that use
# the refresh cookie (double-submit: a cross-site form cannot read our cookie,
# so it cannot forge the header). SameSite=Lax keeps the cookie off cross-site
# POSTs as well. Both cookies are scoped to /api/auth and are only ever sent
# same-origin: the Vite dev server proxies /api to the API, nginx does the
# same in production. `Secure` is set outside development.

REFRESH_COOKIE = "autobuy_refresh"
CSRF_COOKIE = "autobuy_csrf"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_PATH = "/api/auth"      # the refresh cookie: only ever sent to the auth endpoints
CSRF_COOKIE_PATH = "/"         # the CSRF cookie must be readable by the page on ANY route
# (document.cookie only exposes cookies whose Path matches the page URL: a CSRF
# cookie scoped to /api/auth is invisible on /dashboard, so after a reload the
# page could not send the header and every refresh answered 403.)


def _cookie_secure() -> bool:
    return (settings.api_env or "").lower() not in ("development", "dev", "test", "testing", "local")


def set_session_cookies(response: Response, refresh_token: str, csrf_token: str) -> None:
    max_age = settings.refresh_token_expire_days * 24 * 3600
    response.set_cookie(REFRESH_COOKIE, refresh_token, max_age=max_age, path=COOKIE_PATH,
                        httponly=True, secure=_cookie_secure(), samesite="lax")
    response.set_cookie(CSRF_COOKIE, csrf_token, max_age=max_age, path=CSRF_COOKIE_PATH,
                        httponly=False, secure=_cookie_secure(), samesite="lax")


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH, httponly=True, secure=_cookie_secure(), samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path=CSRF_COOKIE_PATH, httponly=False, secure=_cookie_secure(), samesite="lax")
    # older builds set the CSRF cookie on /api/auth: clear that copy too
    response.delete_cookie(CSRF_COOKIE, path=COOKIE_PATH, httponly=False, secure=_cookie_secure(), samesite="lax")


CSRF_MISMATCH = "header does not match the CSRF cookie"


def _csrf_problem(request: Request) -> Optional[str]:
    """None when the double-submit check passes, else the reason."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if cookie and header and secrets.compare_digest(cookie, header):
        return None
    return "no CSRF cookie" if not cookie else "no X-CSRF-Token header" if not header else CSRF_MISMATCH


def _csrf_failed(request: Request, reason: str) -> HTTPException:
    logger.warning("csrf_failed", extra={"path": request.url.path, "reason": reason,
                                         "has_refresh_cookie": bool(request.cookies.get(REFRESH_COOKIE))})
    return HTTPException(status_code=403, detail={"error": "csrf_failed",
                                                 "details": {"message": f"{reason}: send {CSRF_HEADER} equal to the {CSRF_COOKIE} cookie"}})


def _require_csrf(request: Request) -> None:
    reason = _csrf_problem(request)
    if reason:
        raise _csrf_failed(request, reason)


def _refused(exc: HTTPException) -> JSONResponse:
    """The response FastAPI's handler would send for `exc`, built here so a
    background task can ride on it: a raised exception drops the request's
    background tasks, a returned response keeps them. Same status, body and
    headers either way, so the two kinds of refusal are indistinguishable."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


def _start_session(response: Response, user_id: str) -> TokenResponse:
    """Access token for the body (memory only) + refresh/CSRF cookies."""
    access: Token = create_access_token(user_id)
    csrf = secrets.token_urlsafe(32)
    set_session_cookies(response, _create_refresh_token(user_id), csrf)
    return TokenResponse(access_token=access.access_token, expires_in=access.expires_in, csrf_token=csrf)


async def _rotate_session(response: Response, user_id: str, prev_jti: Optional[str], prev_csrf: Optional[str] = None) -> TokenResponse:
    """Refresh: a new session whose refresh token names the one it replaced; the
    same session is cached for the grace window under the replaced token's id."""
    import time as _time
    access: Token = create_access_token(user_id)
    csrf = secrets.token_urlsafe(32)
    refresh_token = _create_refresh_token(user_id, prev_jti)
    set_session_cookies(response, refresh_token, csrf)
    if prev_jti:
        new_jti = jwt.get_unverified_claims(refresh_token).get("jti")
        await _store_grace(prev_jti, {"access_token": access.access_token, "access_expires_at": _time.time() + access.expires_in,
                                      "refresh_token": refresh_token, "refresh_jti": new_jti, "csrf_token": csrf,
                                      "prev_csrf": prev_csrf or "", "issued_at": _time.time()})
    return TokenResponse(access_token=access.access_token, expires_in=access.expires_in, csrf_token=csrf)


def _resume_session(response: Response, cached: dict) -> TokenResponse:
    """Grace: hand the second tab the very same session the first one got."""
    import time as _time
    set_session_cookies(response, cached["refresh_token"], cached["csrf_token"])
    return TokenResponse(access_token=cached["access_token"], csrf_token=cached["csrf_token"],
                         expires_in=max(int(cached["access_expires_at"] - _time.time()), 1))


def _token_pair(user_id: str) -> TokenResponse:   # kept for callers/tests that only need an access token
    access: Token = create_access_token(user_id)
    return TokenResponse(access_token=access.access_token, expires_in=access.expires_in, csrf_token="")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def _signup_gated() -> bool:
    """Private beta: gated whenever a code is configured, and always in production."""
    return bool(settings.signup_invite_code) or (settings.api_env or "").lower() == "production"


def invite_message() -> str:
    return f"Signup is by invitation. Contact {settings.support_email or 'support'}."


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, response: Response, payload: RegisterRequest,
                   background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Register a new user with email and password. During the private beta
    the request must carry the invite code; the refusal is the same whatever
    was wrong (missing, mismatched, or no code configured in production), so
    it never reveals whether the code or the email was the problem."""
    email = payload.email.lower()
    if _signup_gated():
        expected = settings.signup_invite_code or ""
        given = payload.invite_code or ""
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            logger.info("signup refused: invitation", extra={"reason": "no code configured" if not expected else "code mismatch"})
            raise HTTPException(status_code=403, detail=invite_message())
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    if _signup_gated():
        logger.info("signup_invite_used", extra={"user_id": str(user.id)})   # the code itself is never logged
    from api.services.email_campaigns import send_welcome
    background.add_task(send_welcome, user.email)
    return user


@router.post("/login", response_model=TokenResponse,
             responses={202: {"model": MfaRequiredResponse,
                              "description": "Password accepted; the account has two-factor authentication on. "
                                             "No session yet and no cookies: finish at /login/mfa."}})
@limiter.limit("5/minute")
async def login(request: Request, response: Response, payload: LoginRequest, background: BackgroundTasks,
                db: AsyncSession = Depends(get_db)):
    """Authenticate with email/password. The body carries the short-lived access
    token (keep it in memory) and a CSRF token; the refresh token is set as an
    httpOnly cookie scoped to /api/auth. An account with two-factor
    authentication on gets **202** `{mfa_required, mfa_token, expires_in}`
    instead, and finishes at /login/mfa."""
    locked_for = await is_locked_out(payload.email)
    if locked_for:
        raise HTTPException(
            status_code=429,
            detail=f"Account locked after repeated failed logins. Try again in {locked_for // 60 + 1} minutes.",
            headers={"Retry-After": str(locked_for)},
        )
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if user is not None and user.password_reset_required:
        # No password yet (an account migrated from Minas AI Guardian, or created by an admin):
        # refused exactly like a wrong password - the same hash check for uniform timing, the
        # same lockout counter, the same answer - and the set-password link is re-sent, at
        # most once an hour (api/services/guardian_identity.py).
        verify_password(payload.password, user.password_hash)
        logger.info("login refused: password not set yet", extra={"user_id": str(user.id)})
        await register_failed_login(payload.email)
        if not user.suspended:
            # a suspended account is refused the same way and gets no link, as at /forgot-password
            background.add_task(guardian_identity.maybe_send_activation, user)
        return _refused(invalid)
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.warning("failed login attempt", extra={"email": payload.email.lower()})
        await register_failed_login(payload.email)
        return _refused(invalid)
    if await twofactor.is_enabled(db, user.id):
        # The password alone does not finish this sign-in, so it does not clear the
        # failed-login counter either: wrong codes at /login/mfa count into the same
        # lockout, and re-posting the password must not reset it between guesses.
        logger.info("login awaiting second factor", extra={"user_id": str(user.id)})
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED,
                            content=MfaRequiredResponse(mfa_token=make_mfa_token(user),
                                                        expires_in=int(MFA_PENDING_TTL.total_seconds())).model_dump())
    await clear_failed_logins(payload.email)
    await _wait_out_revocation_second(user)
    return _start_session(response, str(user.id))


# --------------------------------------------------------------------------
# Second factor (v0.9.0)
# --------------------------------------------------------------------------
# A sign-in that passed the password but owes a code holds a short-lived
# `mfa_pending` token. It is not a credential: the bearer guard and /verify
# refuse it, it is redeemable once, it allows a handful of attempts, and it
# dies with the password it was issued against (the `pv` fingerprint).

MFA_USED_PREFIX = "mfa-used:"
MFA_ATTEMPTS_PREFIX = "mfa-attempts:"
MFA_MAX_ATTEMPTS = 5


async def _wait_out_revocation_second(user: User) -> None:
    """A JWT iat has one-second resolution and a token is refused when iat < the
    cutoff (last password change or "sign out everywhere"). A session minted later
    in the same second as that cutoff would be dead on arrival, so wait for the
    next whole second first (at most one second, and only in that one case)."""
    cutoff = token_cutoff(user)
    if cutoff is None:
        return
    wait = (cutoff.replace(microsecond=0) + timedelta(seconds=1) - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        await asyncio.sleep(min(wait, 1.0))


def make_mfa_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user.id), "type": "mfa_pending", "jti": uuid4().hex,
                       "pv": _password_version(user.password_hash),
                       "iat": now, "exp": now + MFA_PENDING_TTL},
                      settings.secret_key, algorithm=settings.algorithm)


async def _mfa_token_spent(jti: str) -> bool:
    from api.services.redis_client import get_redis
    try:
        return bool(await get_redis().exists(MFA_USED_PREFIX + jti))
    except Exception:
        logger.warning("mfa token replay check unavailable (Redis down); allowing")
        return False


async def _mfa_attempt_allowed(jti: str) -> bool:
    """At most MFA_MAX_ATTEMPTS codes per pending token, whatever the lockout state."""
    from api.services.redis_client import get_redis
    try:
        r = get_redis()
        count = await r.incr(MFA_ATTEMPTS_PREFIX + jti)
        if count == 1:
            await r.expire(MFA_ATTEMPTS_PREFIX + jti, int(MFA_PENDING_TTL.total_seconds()))
        return count <= MFA_MAX_ATTEMPTS
    except Exception:
        logger.warning("mfa attempt counter unavailable (Redis down); allowing")
        return True


async def _mark_mfa_token_spent(jti: str, exp_ts: int) -> None:
    from api.services.redis_client import get_redis
    import time as _time
    try:
        await get_redis().setex(MFA_USED_PREFIX + jti, max(int(exp_ts - _time.time()), 1), "1")
    except Exception:
        logger.warning("could not record spent mfa token (Redis down)")


@router.post("/login/mfa", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login_mfa(request: Request, response: Response, payload: MfaLoginRequest,
                    background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Second step of sign-in for an account with two-factor authentication on:
    the `mfa_token` from the 202 answer of /login plus either a six-digit
    authenticator `code` or one of the account's `recovery_code`s. Answers the
    same body and cookies as /login. The token is single-use, lives five
    minutes, allows five attempts and dies if the password changes; a wrong
    code counts towards the account lockout exactly like a wrong password."""
    start_again = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid or expired sign-in. Start again from the login page.",
                                headers={"WWW-Authenticate": "Bearer"})
    try:
        decoded = jwt.decode(payload.mfa_token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        raise start_again
    if decoded.get("type") != "mfa_pending" or not decoded.get("sub") or not decoded.get("jti"):
        raise start_again
    try:
        user_id = UUID(decoded["sub"])
    except (ValueError, TypeError):
        raise start_again
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or decoded.get("pv") != _password_version(user.password_hash):
        raise start_again
    if user.suspended:
        raise HTTPException(status_code=403, detail="Account suspended — contact support")
    if user.password_reset_required:
        # see login(): an account without a password cannot finish a sign-in either
        logger.info("login refused at the second step: password not set yet", extra={"user_id": str(user.id)})
        await register_failed_login(user.email)
        background.add_task(guardian_identity.maybe_send_activation, user)
        return _refused(start_again)
    if not await twofactor.is_enabled(db, user.id):
        raise start_again
    locked_for = await is_locked_out(user.email)
    if locked_for:
        raise HTTPException(
            status_code=429,
            detail=f"Account locked after repeated failed logins. Try again in {locked_for // 60 + 1} minutes.",
            headers={"Retry-After": str(locked_for)},
        )
    jti = decoded["jti"]
    if await _mfa_token_spent(jti) or not await _mfa_attempt_allowed(jti):
        raise start_again
    verdict = await twofactor.check_second_factor(db, user, code=payload.code, recovery_code=payload.recovery_code)
    if not verdict.ok:
        logger.warning("mfa_code_rejected", extra={"user_id": str(user.id)})
        await register_failed_login(user.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="That code was not accepted. Check the time on your phone and enter the current code.",
                            headers={"WWW-Authenticate": "Bearer"})
    await clear_failed_logins(user.email)
    await _mark_mfa_token_spent(jti, int(decoded.get("exp", 0)))
    if verdict.used_recovery_code:
        logger.info("mfa_recovery_code_used", extra={"user_id": str(user.id),
                                                     "remaining": verdict.recovery_codes_remaining})
        from api.routes.twofactor import send_two_factor_notice
        background.add_task(send_two_factor_notice, user.email, "recovery_code_used",
                            when=datetime.now(timezone.utc), ip=_client_ip(request),
                            remaining=verdict.recovery_codes_remaining)
    await _wait_out_revocation_second(user)
    return _start_session(response, str(user.id))


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
@limiter.limit("5/minute", key_func=user_or_ip_key)
async def logout_all(request: Request, response: Response, current_user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    """Sign out everywhere: every access and refresh token issued before now
    stops working, including the one making this request, and the session
    cookies are cleared. Needs a valid bearer token and no CSRF header: a
    cross-site page cannot forge the Authorization header."""
    current_user.sessions_revoked_at = datetime.now(timezone.utc)
    await db.commit()
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        try:
            claims = jwt.decode(raw, settings.secret_key, algorithms=[settings.algorithm])
        except JWTError:
            claims = None
        if claims and claims.get("type") == "refresh" and claims.get("jti"):
            try:
                await _revoke(claims["jti"], int(claims.get("exp", 0)))
            except Exception:
                logger.warning("refresh token not revoked in Redis (down); sessions_revoked_at still refuses it")
            await _drop_grace(claims.get("prv"))
            await _drop_grace(claims["jti"])
    logger.info("sessions_revoked", extra={"user_id": str(current_user.id)})
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(out)
    return out


# Tokens that name an account without being a credential. The bearer guard
# (api/routes/user.py) refuses them; /verify says `valid: false` for the same
# ones, so a client cannot mistake a sign-in step or a set-password link for a
# session. A password-reset token lives seven days when it is issued as an
# activation link (api/services/guardian_identity.py). The email-verification
# token stays `valid: true`: its page uses /verify to learn whose link it is.
_NOT_A_CREDENTIAL = frozenset({"mfa_pending", "password_reset"})


def _token_type(token: str) -> Optional[str]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm]).get("type")
    except JWTError:
        return None


@router.post("/verify", response_model=VerifyResponse)
@limiter.limit("10/minute")
async def verify(request: Request, response: Response, payload: VerifyRequest):
    """Verify an access token; returns validity and the user id it belongs to.
    `valid: false` for the two-factor pending token and for a password-reset
    token (the set-password link included): neither is a credential."""
    data = verify_token(payload.token)
    if data is None or data.user_id is None or _token_type(payload.token) in _NOT_A_CREDENTIAL:
        return VerifyResponse(valid=False)
    return VerifyResponse(valid=True, user_id=data.user_id)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("10/minute")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Silent session refresh: needs the refresh cookie and the X-CSRF-Token
    header matching the CSRF cookie. Answers a new access token and rotates
    both cookies (the previous refresh token is revoked). 401 without a valid
    cookie, 403 without a valid CSRF header."""
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    def dead(reason: str) -> JSONResponse:
        # A raised HTTPException would drop the injected response's Set-Cookie
        # headers, so build the 401 explicitly and clear the dead cookies on it.
        logger.info("refresh rejected", extra={"reason": reason})
        out = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                           content={"detail": "Invalid or expired refresh token"},
                           headers={"WWW-Authenticate": "Bearer"})
        clear_session_cookies(out)
        return out

    csrf_reason = _csrf_problem(request)
    if csrf_reason and csrf_reason != CSRF_MISMATCH:
        raise _csrf_failed(request, csrf_reason)
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        if csrf_reason:
            raise _csrf_failed(request, csrf_reason)
        logger.info("refresh rejected", extra={"reason": "no refresh cookie"})
        raise invalid
    try:
        decoded = jwt.decode(raw, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as exc:
        return dead(f"refresh token invalid or expired: {type(exc).__name__}")
    if decoded.get("type") != "refresh" or not decoded.get("sub"):
        raise invalid
    try:
        user_id = UUID(decoded["sub"])
    except (ValueError, TypeError):
        raise invalid

    jti = decoded.get("jti")

    # Make sure the user still exists before minting new tokens.
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return dead("user no longer exists")
    cutoff = token_cutoff(user)   # the last password change or "sign out everywhere", whichever is later
    if cutoff is not None and decoded.get("iat") is not None:
        issued = datetime.fromtimestamp(decoded["iat"], tz=timezone.utc)
        if issued < cutoff:
            await _drop_grace(jti)   # nothing minted for this token may survive the change either
            return dead("refresh token issued before the password was changed")

    if csrf_reason:
        # Two tabs reloaded together: this one built its request with the CSRF value from BEFORE the
        # other tab's rotation, but the browser attached the rotated cookies when it was sent. Inside
        # the grace window the rotation's cached entry (keyed by the token this cookie replaced) knows
        # that older CSRF value; serve the same session the first tab got. Anything else is a real mismatch.
        header = request.headers.get(CSRF_HEADER) or ""
        cached = await _load_grace(decoded.get("prv")) if decoded.get("prv") else None
        if (cached and cached.get("prev_csrf") and secrets.compare_digest(cached["prev_csrf"], header)
                and cached.get("refresh_jti") == jti
                and not (cutoff is not None and cached["issued_at"] < cutoff.timestamp())):
            logger.info("refresh served from the rotation grace window (previous CSRF value)")
            return _resume_session(response, cached)
        raise _csrf_failed(request, csrf_reason)

    if jti:
        claimed = await _claim_rotation(jti, int(decoded.get("exp", 0)))   # atomic: exactly one request rotates
        if claimed is False:
            cached = await _wait_for_grace(jti)
            if cached is None:
                return dead("refresh token revoked (logout or rotation)")
            if cutoff is not None and cached["issued_at"] < cutoff.timestamp():
                await _drop_grace(jti)
                return dead("refresh token issued before the password was changed")
            if cached.get("refresh_jti") and await _is_revoked(cached["refresh_jti"]):
                await _drop_grace(jti)
                return dead("refresh token revoked (logout or rotation)")
            logger.info("refresh served from the rotation grace window")
            return _resume_session(response, cached)
    await _drop_grace(decoded.get("prv"))   # the token before this one is now two rotations old: no grace for it
    return await _rotate_session(response, str(user_id), jti, request.cookies.get(CSRF_COOKIE))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
@limiter.limit("10/minute")
async def logout(request: Request, response: Response):
    """Sign out: revoke the refresh token from the cookie server-side and clear
    both session cookies. The short-lived access token simply expires. Always
    204 once the cookie holds a valid refresh token, **even when the CSRF
    header does not match**: the cookies are SameSite=Lax, so a cross-site
    POST cannot carry them and a forged sign-out is not possible, while a tab
    whose CSRF value went stale after another tab rotated the cookies must
    still be able to sign out for real (issue #64). Without a valid refresh
    cookie the CSRF check applies as before (there is nothing to revoke)."""
    raw = request.cookies.get(REFRESH_COOKIE)
    decoded = None
    if raw:
        try:
            candidate = jwt.decode(raw, settings.secret_key, algorithms=[settings.algorithm])
            if candidate.get("type") == "refresh" and candidate.get("jti"):
                decoded = candidate
        except JWTError:
            decoded = None   # nothing valid to revoke; the cookie is cleared anyway
    if decoded is None:
        _require_csrf(request)
    else:
        csrf_reason = _csrf_problem(request)
        if csrf_reason:
            logger.info("logout accepted despite a CSRF problem: the refresh cookie is valid and is revoked",
                        extra={"reason": csrf_reason})
        await _revoke(decoded["jti"], int(decoded.get("exp", 0)))
        await _drop_grace(decoded.get("prv"))   # a tab inside the grace window must not get this session back
        await _drop_grace(decoded["jti"])
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(out)
    return out


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    _strength = field_validator("new_password")(validate_password_strength)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    background: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the password and invalidate every token issued before now."""
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    from api.services import wallet_hold
    current_user.password_hash = hash_password(payload.new_password)
    current_user.password_changed_at = datetime.now(timezone.utc)
    hold_until = wallet_hold.start_hold(current_user, "password_change", current_user.password_changed_at)
    await db.commit()
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:   # the grace entry that would still hand out the pre-change session
        try:
            claims = jwt.get_unverified_claims(raw)
            await _drop_grace(claims.get("prv"))
            await _drop_grace(claims.get("jti"))
        except JWTError:
            pass
    logger.info("password changed", extra={"user_id": str(current_user.id), "wallet_hold_until": hold_until.isoformat()})
    background.add_task(_send_password_changed_email, current_user.email,
                        current_user.password_changed_at, _client_ip(request), hold_until)


# --------------------------------------------------------------------------
# Password reset (forgot password)
# --------------------------------------------------------------------------

def _password_version(password_hash: str) -> str:
    """Fingerprint of the current hash: a reset token embeds it, so the token
    dies the moment the password changes (single use, and useless after a
    normal password change)."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_password_reset_token(user: User, ttl: timedelta = PASSWORD_RESET_TTL) -> str:
    """JWT (type=password_reset) bound to the user's current password: one hour
    for a forgotten password, `ttl` for the seven-day set-password link of an
    account provisioned without one (api/services/guardian_identity.py).
    Rejected by the bearer guard, so a leaked link cannot log anyone in."""
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user.id), "type": "password_reset", "jti": uuid4().hex,
                       "pv": _password_version(user.password_hash),
                       "iat": now, "exp": now + ttl},
                      settings.secret_key, algorithm=settings.algorithm)


def password_reset_link(token: str) -> str:
    """Built only from APP_BASE_URL (never the request Host)."""
    return build_app_link("/reset-password", token=token)


async def _send_password_reset_email(email: str, link: str) -> None:
    app = settings.app_name
    text = (f"We received a request to reset the password for your {app} account.\n\n"
            f"Open this link within 1 hour to choose a new password:\n{link}\n\n"
            "If you did not request this, you can ignore this email; your password will not change.")
    html = (f"<p>We received a request to reset the password for your {app} account.</p>"
            f"<p><a href=\"{link}\">Reset my password</a> (valid for 1 hour)</p>"
            "<p>If you did not request this, you can ignore this email; your password will not change.</p>")
    # Security category: queued through the outbox (next drain within a minute) with 6 delivery
    # attempts over ~15 minutes, instead of one inline attempt that a provider blip lost for good.
    if not await email_service.queue(email, f"Reset your {app} password", html, text, template="password_reset",
                                     category="security"):
        logger.error("password reset email not delivered to %s: outbox unavailable and inline attempt failed", email)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _send_password_changed_email(email: str, when: datetime, ip: str, hold_until: Optional[datetime] = None) -> None:
    """Security notice after a reset or a change (never optional, never affects
    the password change itself). Mentions the 24-hour hold on new withdrawal addresses."""
    app = settings.app_name
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    contact = f"contact {settings.support_email} immediately" if settings.support_email else "contact support immediately"
    hold = ""
    if hold_until is not None:
        ends = hold_until.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        hold = (f" For your safety, new withdrawal addresses are on hold until {ends}: until then you cannot add a new "
                "wallet address, switch a purchase to withdraw-immediately, or start an autobuy purchase to an address you "
                "have not used before. Purchases to addresses you already use keep running.")
    text = (f"The password for your {app} account was changed.\n\n"
            f"When: {stamp}\nFrom IP address: {ip}\n\n"
            f"Every other session has been signed out.{hold} If this wasn't you, {contact}.")
    html = (f"<p>The password for your {app} account was changed.</p>"
            f"<p><strong>When:</strong> {stamp}<br/><strong>From IP address:</strong> {ip}</p>"
            f"<p>Every other session has been signed out.{hold} If this wasn't you, {contact}.</p>")
    if not await email_service.queue(email, f"Your {app} password was changed", html, text,
                                     template="password_changed", category="security"):
        logger.error("password changed notice not delivered to %s: outbox unavailable and inline attempt failed", email)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
@limiter.limit("3/minute")
async def forgot_password(request: Request, response: Response, payload: ForgotPasswordRequest,
                          background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Email a one-hour password reset link. Always answers `{sent: true}`
    so the response cannot be used to discover which emails have accounts."""
    email = payload.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and not user.suspended:
        link = password_reset_link(make_password_reset_token(user))
        background.add_task(_send_password_reset_email, user.email, link)
        logger.info("password reset requested", extra={"user_id": str(user.id)})
        if settings.reset_links_logged:  # explicit opt-in only, never tied to the environment name
            logger.warning("password reset link (LOG_RESET_LINKS=1): %s", link)
    else:
        logger.info("password reset requested for unknown or suspended email")
    return ForgotPasswordResponse(sent=True)


async def _resolve_reset_token(token: str, db: AsyncSession):
    """Every check a reset token must pass, without consuming it.
    Returns (decoded claims, user) or None when the token is unusable."""
    try:
        decoded = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None
    if decoded.get("type") != "password_reset" or not decoded.get("sub") or not decoded.get("jti"):
        return None
    try:
        user_id = UUID(decoded["sub"])
    except (ValueError, TypeError):
        return None
    if await _is_reset_used(decoded["jti"]):
        return None
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.suspended or decoded.get("pv") != _password_version(user.password_hash):
        return None
    return decoded, user


@router.post("/reset-password/validate", response_model=ResetTokenValidity,
             responses={400: {"model": ResetTokenValidity, "description": "Token is invalid, expired or already used"}})
@limiter.limit("10/minute")
async def validate_reset_token(request: Request, response: Response, payload: ResetTokenRequest,
                               db: AsyncSession = Depends(get_db)):
    """Check a reset token without consuming it: 200 `{valid: true}` when
    `/reset-password` would accept it, 400 `{valid: false}` otherwise. Lets
    the reset page show the expired-link state before asking for a password."""
    if await _resolve_reset_token(payload.token, db) is None:
        return JSONResponse(status_code=400, content={"valid": False})
    return ResetTokenValidity(valid=True)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
@limiter.limit("5/minute")
async def reset_password(request: Request, response: Response, payload: ResetPasswordRequest,
                         background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Set a new password using a reset token from `/forgot-password`.
    The token is single-use and expires after an hour; on success every
    existing session is signed out."""
    resolved = await _resolve_reset_token(payload.token, db)
    if resolved is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    decoded, user = resolved
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=422, detail="New password must differ from the current password")

    from api.services import wallet_hold
    user.password_hash = hash_password(payload.new_password)
    user.password_reset_required = False   # a provisioned account now has a password of its own
    user.password_changed_at = datetime.now(timezone.utc)
    hold_until = wallet_hold.start_hold(user, "password_reset", user.password_changed_at)
    await db.commit()
    await _mark_reset_used(decoded["jti"], int(decoded.get("exp", 0)))
    logger.info("password reset completed", extra={"user_id": str(user.id), "wallet_hold_until": hold_until.isoformat()})
    background.add_task(_send_password_changed_email, user.email, user.password_changed_at, _client_ip(request), hold_until)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _is_reset_used(jti: str) -> bool:
    from api.services.redis_client import get_redis
    try:
        return bool(await get_redis().exists(RESET_USED_PREFIX + jti))
    except Exception:
        return False  # the password-version check still prevents reuse


async def _mark_reset_used(jti: str, exp_ts: int) -> None:
    from api.services.redis_client import get_redis
    try:
        ttl = max(exp_ts - int(datetime.now(timezone.utc).timestamp()), 60)
        await get_redis().setex(RESET_USED_PREFIX + jti, ttl, "1")
    except Exception:
        logger.warning("could not record used reset token (Redis down)")
