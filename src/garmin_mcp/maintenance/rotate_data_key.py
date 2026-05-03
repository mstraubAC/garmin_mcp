"""Fernet data-key rotation CLI for the Garmin MCP proxy.

Re-encrypts every `garmin_tokens` row from the old key to the new key
in a single SQLite transaction. If any row fails to decrypt, the
transaction is rolled back and the database is left unchanged.

Usage (offline — stop the service first):
    export GARMIN_MCP_OLD_KEY="<current-key>"
    export GARMIN_MCP_NEW_KEY="<new-key>"
    garmin-mcp-rotate-data-key [--db-path /var/lib/garmin-mcp/state.db]

Keys are read from env vars, never CLI arguments, to prevent leakage
via `ps aux` or shell history.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_DB_PATH = "/var/lib/garmin-mcp/state.db"


def _validate_key(label: str, key: str) -> Fernet:
    """Validate a Fernet key and return the Fernet instance."""
    if not key:
        print(f"error: {label} is empty or not set", file=sys.stderr)
        sys.exit(1)
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as e:
        print(f"error: {label} is not a valid Fernet key: {e}", file=sys.stderr)
        sys.exit(1)


def rotate(db_path: str, old_key: str, new_key: str) -> int:
    """Rotate all garmin_tokens rows from `old_key` to `new_key`.

    Returns the number of rows rotated.
    """
    old_fernet = _validate_key("GARMIN_MCP_OLD_KEY", old_key)
    new_fernet = _validate_key("GARMIN_MCP_NEW_KEY", new_key)

    if old_key == new_key:
        print("error: old and new keys are identical — nothing to do", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        rows = conn.execute(
            "SELECT user_id, encrypted_blob FROM garmin_tokens"
        ).fetchall()

        if not rows:
            print("No garmin_tokens rows found — nothing to rotate.")
            return 0

        # Decrypt all rows with old key first (fail fast if any row is bad).
        to_update: list[tuple[str, bytes]] = []
        for row in rows:
            user_id = row["user_id"]
            encrypted = bytes(row["encrypted_blob"])
            try:
                plaintext = old_fernet.decrypt(encrypted)
            except InvalidToken:
                print(
                    f"error: failed to decrypt token for user {user_id} "
                    "with GARMIN_MCP_OLD_KEY — key mismatch or corrupted data. "
                    "Aborting, no rows modified.",
                    file=sys.stderr,
                )
                conn.rollback()
                conn.close()
                sys.exit(1)
            to_update.append((user_id, new_fernet.encrypt(plaintext)))

        # All rows verified — apply updates in a single transaction.
        with conn:
            for user_id, new_blob in to_update:
                conn.execute(
                    "UPDATE garmin_tokens SET encrypted_blob = ?, "
                    "updated_at = unixepoch() WHERE user_id = ?",
                    (new_blob, user_id),
                )

        print(f"Rotated {len(to_update)} garmin_tokens row(s) to the new key.")
        return len(to_update)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate the Fernet data key for encrypted Garmin tokens."
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite state database (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    old_key = os.environ.get("GARMIN_MCP_OLD_KEY", "")
    new_key = os.environ.get("GARMIN_MCP_NEW_KEY", "")

    if not old_key or not new_key:
        print(
            "error: both GARMIN_MCP_OLD_KEY and GARMIN_MCP_NEW_KEY must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    rotate(args.db_path, old_key, new_key)


if __name__ == "__main__":
    main()
