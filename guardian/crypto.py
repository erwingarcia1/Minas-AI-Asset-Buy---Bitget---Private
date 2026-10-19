"""Guardian secrets under the platform cipher.

Guardian encrypted keys with a raw Fernet key from SENTINEL_MASTER_KEY. In the
platform every secret at rest (exchange keys, TOTP secrets, monitoring keys)
uses one cipher, api/services/crypto.py: Fernet keyed by sha256(ENCRYPTION_KEY),
rotated by scripts/rotate_encryption_key.py. SENTINEL_MASTER_KEY survives only
as an input to the one-off cutover script, which decrypts with it and
re-encrypts with this.

The class keeps Guardian's name and interface so the engine, the onboarding
path and their tests are unchanged: `encrypt`, `decrypt`, and a RuntimeError
on a wrong key.
"""
from __future__ import annotations

from api.services.crypto import InvalidToken, decrypt_value, encrypt_value


class CredentialCipher:
    """Decrypted values must only live in worker memory for the duration of a
    single exchange call."""

    def __init__(self, master_key: str | None = None):
        # Accepted for signature compatibility with Guardian's callers; the key
        # is the platform's ENCRYPTION_KEY, never a per-instance value.
        self._ignored = master_key

    def encrypt(self, plaintext: str) -> str:
        return encrypt_value(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        try:
            return decrypt_value(ciphertext)
        except InvalidToken as exc:
            raise RuntimeError("Failed to decrypt credential - wrong master key?") from exc


PlatformCipher = CredentialCipher


def redact_secrets(text: str, *secrets: str | None) -> str:
    """Strip known secret values out of a string before it is logged or
    included in an error message/alert."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
