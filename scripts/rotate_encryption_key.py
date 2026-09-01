"""Re-encrypt all stored exchange API keys under a new ENCRYPTION_KEY.

Usage:
    OLD_ENCRYPTION_KEY=<previous key material> ENCRYPTION_KEY=<new key material> \
        python scripts/rotate_encryption_key.py

OLD_ENCRYPTION_KEY may be empty ("") when rotating off the legacy
SECRET_KEY-derived cipher — the script then uses SECRET_KEY as the old material.
Run with the API stopped; the change is transactional (all rows or none).
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
        rows = conn.execute(
            text("SELECT id, encrypted_api_key, encrypted_api_secret FROM api_keys")).all()
        for row in rows:
            conn.execute(
                text("UPDATE api_keys SET encrypted_api_key=:k, encrypted_api_secret=:s "
                     "WHERE id=:id"),
                {"id": row.id,
                 "k": new_f.encrypt(old_f.decrypt(row.encrypted_api_key.encode())).decode(),
                 "s": new_f.encrypt(old_f.decrypt(row.encrypted_api_secret.encode())).decode()})
        print(f"re-encrypted {len(rows)} api_keys row(s)")


if __name__ == "__main__":
    main()
