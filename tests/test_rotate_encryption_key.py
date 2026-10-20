"""scripts/rotate_encryption_key.py re-encrypts every stored secret, the Bitget passphrase included.

A rotation that skipped encrypted_passphrase would leave every Bitget key unable to
sign a request after the restart: no buy would go through until the key was re-entered."""
import importlib.util
import pathlib

from sqlalchemy import delete, select, text

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


async def test_a_removed_guardian_account_with_scrubbed_ciphertext_does_not_break_the_rotation(db, user, monkeypatch):
    """scrub_and_delete leaves the row with '' in its encrypted columns; decrypting '' raises,
    which would fail the whole (all-or-nothing) rotation. A live Guardian row rotates, a scrubbed
    one is left alone."""
    live_id = (await db.execute(text(
        "INSERT INTO guardian_accounts (user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, "
        "encrypted_passphrase, status, onboarding_source, onboarded_at, updated_at) VALUES (:u, 'rot-live', 'bitget', "
        "'independent', :k, :s, :p, 'active', 'self', now(), now()) RETURNING id"),
        {"u": user.id, "k": encrypt_value("g-key"), "s": encrypt_value("g-secret"), "p": encrypt_value("g-pass")})).scalar_one()
    scrubbed_id = (await db.execute(text(
        "INSERT INTO guardian_accounts (user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, "
        "encrypted_passphrase, status, onboarding_source, onboarded_at, updated_at) VALUES (:u, 'rot-scrubbed', 'bitget', "
        "'independent', '', '', '', 'disabled', 'self', now(), now()) RETURNING id"), {"u": user.id})).scalar_one()
    await db.commit()
    current = rotate.settings.encryption_key or rotate.settings.secret_key
    new_material = "rotation-test-new-key-material-2"
    try:
        monkeypatch.setenv("OLD_ENCRYPTION_KEY", current)
        monkeypatch.setattr(rotate.settings, "encryption_key", new_material)
        rotate.main()   # must not raise on the scrubbed row
        new_f = rotate.fernet_for(new_material)
        rows = (await db.execute(text("SELECT id, encrypted_api_key, encrypted_passphrase FROM guardian_accounts WHERE id IN (:a, :b)")
                                 .execution_options(populate_existing=True), {"a": live_id, "b": scrubbed_id})).all()
        by_id = {r.id: r for r in rows}
        assert new_f.decrypt(by_id[live_id].encrypted_api_key.encode()).decode() == "g-key"
        assert new_f.decrypt(by_id[live_id].encrypted_passphrase.encode()).decode() == "g-pass"
        assert by_id[scrubbed_id].encrypted_api_key == "" and by_id[scrubbed_id].encrypted_passphrase == ""
    finally:
        monkeypatch.setenv("OLD_ENCRYPTION_KEY", new_material)
        monkeypatch.setattr(rotate.settings, "encryption_key", current)
        rotate.main()
        await db.execute(text("DELETE FROM guardian_accounts WHERE id IN (:a, :b)"), {"a": live_id, "b": scrubbed_id})
        await db.commit()
