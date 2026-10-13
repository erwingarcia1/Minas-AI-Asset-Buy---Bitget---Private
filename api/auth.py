from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from api.config import settings

# Password hashing context
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__time_cost=2,
    argon2__memory_cost=65536,
    argon2__parallelism=1,
)

# A sign-in that passed the password but still owes a second factor lives this long.
MFA_PENDING_TTL = timedelta(minutes=5)

# Token schemas
class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int

class TokenData(BaseModel):
    user_id: Optional[str] = None

# Password functions
def hash_password(password: str) -> str:
    """Hash a password using Argon2."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a hashed password."""
    return pwd_context.verify(plain_password, hashed_password)

# JWT functions
def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> Token:
    """Create a JWT access token."""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc)
    }
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.secret_key,
        algorithm=settings.algorithm
    )
    
    return Token(
        access_token=encoded_jwt,
        token_type="bearer",
        expires_in=int(expires_delta.total_seconds())
    )

def verify_token(token: str) -> Optional[TokenData]:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm]
        )
        user_id: str = payload.get("sub")
        if user_id is None or payload.get("type") == "mfa_pending":
            return None   # a sign-in that still owes its second factor identifies nobody yet
        return TokenData(user_id=user_id)
    except JWTError:
        return None


def token_cutoff(user) -> Optional[datetime]:
    """Tokens issued before this instant are dead: the later of the last
    password change and the last "sign out everywhere" (sessions_revoked_at,
    migration 029). None when neither has happened."""
    stamps = [t for t in (getattr(user, "password_changed_at", None), getattr(user, "sessions_revoked_at", None))
              if t is not None]
    return max(stamps) if stamps else None
