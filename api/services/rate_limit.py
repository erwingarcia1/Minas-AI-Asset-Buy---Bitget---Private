"""Rate limiting (slowapi/Redis) and login lockout.

Keys: unauthenticated routes limit per client IP (request.client only —
X-Forwarded-For is deliberately ignored, so spoofed headers cannot dodge a
limit); authenticated routes limit per JWT subject, falling back to IP.
Set RATE_LIMIT_ENABLED=0 to disable (tests, local load runs).
"""
import logging

from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.config import settings

logger = logging.getLogger("autobuy.ratelimit")

LOCKOUT_FAILS = 5
LOCKOUT_WINDOW_S = 600
LOCKOUT_DURATION_S = 900


def user_or_ip_key(request) -> str:
    """Per-user key for authenticated routes; anonymous callers share their IP."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = jwt.decode(auth[7:], settings.secret_key,
                                 algorithms=[settings.algorithm])
            if payload.get("sub"):
                return f"user:{payload['sub']}"
        except JWTError:
            pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=get_remote_address,          # default: per-IP
    default_limits=["60/minute"],
    storage_uri=settings.redis_url,
    headers_enabled=True,                 # X-RateLimit-* + Retry-After on 429
    enabled=settings.rate_limit_enabled,
)


# ---------------------------------------------------------------- login lockout

def _keys(email: str) -> tuple:
    email = email.lower()
    return f"login-fails:{email}", f"login-lockout:{email}"


async def is_locked_out(email: str) -> int:
    """Seconds the account stays locked, or 0. Fails open if Redis is down."""
    from api.services.redis_client import get_redis
    try:
        ttl = await get_redis().ttl(_keys(email)[1])
        return max(ttl, 0)
    except Exception:
        logger.warning("lockout check unavailable (Redis down)")
        return 0


async def register_failed_login(email: str) -> None:
    from api.services.redis_client import get_redis
    fails_key, lockout_key = _keys(email)
    try:
        r = get_redis()
        fails = await r.incr(fails_key)
        if fails == 1:
            await r.expire(fails_key, LOCKOUT_WINDOW_S)
        if fails >= LOCKOUT_FAILS:
            await r.setex(lockout_key, LOCKOUT_DURATION_S, "1")
            await r.delete(fails_key)
            logger.warning("account locked after repeated failed logins",
                           extra={"email": email.lower()})
    except Exception:
        logger.warning("failed-login tracking unavailable (Redis down)")


async def clear_failed_logins(email: str) -> None:
    from api.services.redis_client import get_redis
    try:
        await get_redis().delete(_keys(email)[0])
    except Exception:
        pass
