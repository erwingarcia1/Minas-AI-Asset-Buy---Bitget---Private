"""scripts/rotate_encryption_key.py re-encrypts every stored secret, the Bitget passphrase included.

A rotation that skipped encrypted_passphrase would leave every Bitget key unable to
sign a request after the restart: no buy would go through until the key was re-entered."""
import importlib.util
import pathlib

from sqlalchemy import delete, select

from api.models import APIKey
from api.services.crypto import decrypt_value, encrypt_value
from tests.conftest_autobuy import db, user  # noqa: F401  (shared fixtures)

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rotate_encryption_key", ROOT / "scripts" / "rotate_encryption_key.py")
rotate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rotate)


def reload(key_id):
    """Re-read the row from the database, overwriting what the session cached (the script wrote through its own connection)."""
    return select(APIKey).where(APIKey.id == key_id).execution_options(populate_existing=True)


async def test_rotation_re_encrypts_key_secret_and_passphrase_and_leaves_a_missing_passphrase_alone(db, user, monkeypatch):
    bitget = APIKey(user_id=user.id, exchange="bitget", encrypted_api_key=encrypt_value("rot-key-1"),
                    encrypted_api_secret=encrypt_value("rot-secret-1"), encrypted_passphrase=encrypt_value("rot-pass-1"),
                    verified=True)
    binance = APIKey(user_id=user.id, exchange="binance", encrypted_api_key=encrypt_value("rot-key-2"),
                     encrypted_api_secret=encrypt_value("rot-secret-2"), verified=True)
    db.add_all([bitget, binance])
    await db.commit()
    current = rotate.settings.encryption_key or rotate.settings.secret_key
    new_material = "rotation-test-new-key-material"
    try:
        monkeypatch.setenv("OLD_ENCRYPTION_KEY", current)
        monkeypatch.setattr(rotate.settings, "encryption_key", new_material)
        rotate.main()
        new_f = rotate.fernet_for(new_material)
        fresh = (await db.execute(reload(bitget.id))).scalar_one()
        assert new_f.decrypt(fresh.encrypted_api_key.encode()).decode() == "rot-key-1"
        assert new_f.decrypt(fresh.encrypted_api_secret.encode()).decode() == "rot-secret-1"
        assert new_f.decrypt(fresh.encrypted_passphrase.encode()).decode() == "rot-pass-1"
        fresh2 = (await db.execute(reload(binance.id))).scalar_one()
        assert fresh2.encrypted_passphrase is None
        assert new_f.decrypt(fresh2.encrypted_api_key.encode()).decode() == "rot-key-2"
    finally:
        # and back under the configured key, so nothing else in the suite is left unreadable
        monkeypatch.setenv("OLD_ENCRYPTION_KEY", new_material)
        monkeypatch.setattr(rotate.settings, "encryption_key", current)
        rotate.main()
    fresh = (await db.execute(reload(bitget.id))).scalar_one()
    assert decrypt_value(fresh.encrypted_passphrase) == "rot-pass-1"
    assert decrypt_value(fresh.encrypted_api_key) == "rot-key-1"
    await db.execute(delete(APIKey).where(APIKey.id.in_([bitget.id, binance.id])))
    await db.commit()
