"""Unit tests for the Fernet data-key rotation CLI."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from garmin_mcp.maintenance.rotate_data_key import rotate


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite DB with the garmin_tokens schema."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS garmin_tokens (
            user_id TEXT PRIMARY KEY,
            encrypted_blob BLOB NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    return str(path)


def _seed(db_path: str, fernet: Fernet, users: dict[str, str]) -> None:
    """Seed the DB with encrypted tokens for the given {user_id: plaintext}."""
    conn = sqlite3.connect(db_path)
    for user_id, plaintext in users.items():
        blob = fernet.encrypt(plaintext.encode("utf-8"))
        conn.execute(
            "INSERT INTO garmin_tokens(user_id, encrypted_blob, updated_at) "
            "VALUES (?, ?, unixepoch())",
            (user_id, blob),
        )
    conn.commit()
    conn.close()


def _decrypt_all(db_path: str, fernet: Fernet) -> dict[str, str]:
    """Read and decrypt all rows, returning {user_id: plaintext}."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT user_id, encrypted_blob FROM garmin_tokens").fetchall()
    conn.close()
    result = {}
    for row in rows:
        result[row["user_id"]] = fernet.decrypt(bytes(row["encrypted_blob"])).decode("utf-8")
    return result


def test_rotate_empty_db(db_path):
    """Rotating an empty DB succeeds with 0 rows."""
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    count = rotate(db_path, k1, k2)
    assert count == 0


def test_rotate_roundtrip(db_path):
    """Rotate → data still decryptable with new key, not old."""
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_f = Fernet(old_key.encode())
    new_f = Fernet(new_key.encode())

    _seed(db_path, old_f, {"u1": "token-a", "u2": "token-b"})

    count = rotate(db_path, old_key, new_key)
    assert count == 2

    # New key can decrypt.
    result = _decrypt_all(db_path, new_f)
    assert result == {"u1": "token-a", "u2": "token-b"}

    # Old key cannot decrypt (raises InvalidToken).
    with pytest.raises(Exception):  # noqa: B017
        _decrypt_all(db_path, old_f)


def test_rotate_wrong_old_key_aborts(db_path):
    """If a row can't be decrypted with the old key, abort with no changes."""
    old_key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _seed(db_path, Fernet(old_key.encode()), {"u1": "token-a"})

    # This should fail because wrong_key can't decrypt.
    with pytest.raises(SystemExit):
        rotate(db_path, wrong_key, new_key)

    # Data still decryptable with original key (no changes).
    result = _decrypt_all(db_path, Fernet(old_key.encode()))
    assert result == {"u1": "token-a"}


def test_rotate_same_key_exits(db_path):
    """Rotating to the same key exits with error."""
    k = Fernet.generate_key().decode()
    with pytest.raises(SystemExit):
        rotate(db_path, k, k)


def test_rotate_invalid_key_exits(db_path):
    """A non-Fernet key string exits with error."""
    with pytest.raises(SystemExit):
        rotate(db_path, "not-a-valid-key", Fernet.generate_key().decode())
