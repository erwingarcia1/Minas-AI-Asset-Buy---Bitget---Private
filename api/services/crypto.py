"""Fernet at rest for every secret the platform stores.

Exchange API keys, the refresh-grace cache and (from v0.9.0) TOTP shared
secrets are encrypted with one cipher: Fernet keyed by the sha256 of
ENCRYPTION_KEY (SECRET_KEY in dev), as a urlsafe-b64 32-byte key. Production
refuses to boot without ENCRYPTION_KEY (api/config.py); rotation is
scripts/rotate_encryption_key.py.

Moved here byte-for-byte from api/routes/exchange.py so services and the
Guardian worker can encrypt without importing a router. The router still
re-exports these names, and keeps its own decrypt_value that turns a bad key
into an HTTP 500; here a bad key raises cryptography's InvalidToken.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401  (re-exported)

from api.config import settings


def _fernet() -> Fernet:
    """Fernet keyed off ENCRYPTION_KEY (falling back to SECRET_KEY in dev),
    sha256-derived to a urlsafe-b64 32-byte key."""
    material = settings.encryption_key or settings.secret_key
    digest = hashlib.sha256(material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value: str) -> str:
    """Encrypt a secret for storage."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    """Decrypt a stored secret. Raises InvalidToken under the wrong key."""
    return _fernet().decrypt(token.encode()).decode()


def mask_api_key(value: str) -> str:
    """Show only the first 4 and last 4 characters of a key."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"
