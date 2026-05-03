"""Unit tests for the encrypted Garmin token store."""

import pytest
from cryptography.fernet import Fernet

from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def store(storage, fernet_key):
    return GarminTokenStore(storage, fernet_key)


def _user(storage, user_id="u1"):
    """Insert a minimal user row so the FK constraint is satisfied."""
    storage.get_or_create_user(
        user_id_factory=lambda: user_id,
        entra_sub=f"sub-{user_id}",
        entra_tid="tid",
        email=None,
        display_name=None,
    )


def test_roundtrip_returns_plaintext(store, storage):
    _user(storage)
    store.save("u1", "garth-token-blob-string")
    assert store.load("u1") == "garth-token-blob-string"


def test_load_unknown_user_returns_none(store):
    assert store.load("nope") is None


def test_save_overwrites_previous(store, storage):
    _user(storage)
    store.save("u1", "old")
    store.save("u1", "new")
    assert store.load("u1") == "new"


def test_delete_removes_token(store, storage):
    _user(storage)
    store.save("u1", "x")
    store.delete("u1")
    assert store.load("u1") is None
    assert store.has("u1") is False


def test_has_reflects_presence(store, storage):
    _user(storage)
    assert store.has("u1") is False
    store.save("u1", "x")
    assert store.has("u1") is True


def test_blob_is_actually_encrypted(store, storage):
    """Sanity check: raw row in SQLite should NOT contain the plaintext."""
    _user(storage)
    store.save("u1", "very-secret-garth-token")
    raw = storage.load_garmin_token("u1")
    assert b"very-secret-garth-token" not in raw


def test_wrong_key_cannot_decrypt(storage, fernet_key):
    _user(storage)
    GarminTokenStore(storage, fernet_key).save("u1", "x")

    other_key = Fernet.generate_key().decode()
    with pytest.raises(ValueError, match="rotated"):
        GarminTokenStore(storage, other_key).load("u1")


def test_empty_key_rejected(storage):
    with pytest.raises(ValueError):
        GarminTokenStore(storage, "")


def test_garbage_key_rejected(storage):
    with pytest.raises(ValueError):
        GarminTokenStore(storage, "not-a-fernet-key")


def test_token_persists_across_storage_reopens(tmp_path, fernet_key):
    s1 = Storage(tmp_path / "state.db")
    s1.get_or_create_user(lambda: "u1", "sub1", "tid", None, None)
    GarminTokenStore(s1, fernet_key).save("u1", "persisted-token")
    s1.close()

    s2 = Storage(tmp_path / "state.db")
    assert GarminTokenStore(s2, fernet_key).load("u1") == "persisted-token"
    s2.close()


def test_storage_user_table_extension():
    """Smoke check: the new garmin_tokens table is created at v2 schema."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(os.path.join(tmp, "state.db"))
        # Should not raise
        s._conn.execute("SELECT user_id, encrypted_blob, updated_at FROM garmin_tokens")
        s.close()
