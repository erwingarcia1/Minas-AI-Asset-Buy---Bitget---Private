"""Re-encrypt every stored secret (exchange API keys, TOTP secrets, Guardian monitoring keys) under a new ENCRYPTION_KEY.

Usage:
    OLD_ENCRYPTION_KEY=<previous key material> ENCRYPTION_KEY=<new key material> \
        python scripts/rotate_encryption_key.py

OLD_ENCRYPTION_KEY may be empty ("") when rotating off the legacy
SECRET_KEY-derived cipher — the script then uses SECRET_KEY as the old material.
Run with the API and the Guardian worker stopped
(`docker compose -p autobuy-platform --profile prod stop api guardian`): a worker left
running decrypts with the old key mid-rotation, and every failure reaches customers as
an alert that their account is not being watched. The change is transactional (all rows
or none).
"""
import base64
import hashlib
import os
import sys

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.config import settings  # noqa: E402


def fernet_for(material: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))


def main() -> None:
    new_material = settings.encryption_key
    if not new_material:
        sys.exit("ENCRYPTION_KEY must be set to the NEW key")
    old_material = os.getenv("OLD_ENCRYPTION_KEY") or settings.secret_key
    old_f, new_f = fernet_for(old_material), fernet_for(new_material)

    url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(url)
    with engine.begin() as conn:
        def rotate(token):
            """None stays None (Binance keys carry no passphrase)."""
            return None if token is None else new_f.encrypt(old_f.decrypt(token.encode())).decode()

        rows = conn.execute(
            text("SELECT id, encrypted_api_key, encrypted_api_secret, encrypted_passphrase FROM api_keys")).all()
        for row in rows:
            conn.execute(
                text("UPDATE api_keys SET encrypted_api_key=:k, encrypted_api_secret=:s, encrypted_passphrase=:p "
                     "WHERE id=:id"),
                {"id": row.id,
                 "k": rotate(row.encrypted_api_key),
                 "s": rotate(row.encrypted_api_secret),
                 "p": rotate(row.encrypted_passphrase)})
        print(f"re-encrypted {len(rows)} api_keys row(s)")
        totp_rows = conn.execute(text("SELECT user_id, encrypted_secret FROM user_totp")).all()
        for row in totp_rows:
            conn.execute(text("UPDATE user_totp SET encrypted_secret=:s WHERE user_id=:id"),
                         {"id": row.user_id, "s": new_f.encrypt(old_f.decrypt(row.encrypted_secret.encode())).decode()})
        print(f"re-encrypted {len(totp_rows)} user_totp row(s)")
        guardian_rows = conn.execute(text("SELECT id, encrypted_api_key, encrypted_secret, encrypted_passphrase FROM guardian_accounts")).all()
        for row in guardian_rows:
            conn.execute(
                text("UPDATE guardian_accounts SET encrypted_api_key=:k, encrypted_secret=:s, encrypted_passphrase=:p WHERE id=:id"),
                {"id": row.id,
                 "k": rotate(row.encrypted_api_key),
                 "s": rotate(row.encrypted_secret),
                 "p": rotate(row.encrypted_passphrase)})   # None for exchanges without a passphrase
        print(f"re-encrypted {len(guardian_rows)} guardian_accounts row(s)")


if __name__ == "__main__":
    main()
