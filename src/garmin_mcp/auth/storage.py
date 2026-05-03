"""SQLite-backed persistence for the OAuth proxy.

All operations are sync (sqlite3 stdlib) wrapped in `asyncio.to_thread()` at
call sites that need an async API. SQLite WAL mode + a connection-per-thread
pattern is fast enough for the loads this proxy will see.

Schema is created on first connect; future migrations can be tacked on by
bumping `SCHEMA_VERSION` and adding ALTER statements.

Tables
------
oauth_clients          DCR-issued client records (Claude apps registering against us)
pending_authorizations Mid-flight Entra exchanges (state -> Claude params)
oauth_codes            Our own auth codes issued to Claude after Entra auth
refresh_tokens         Long-lived refresh tokens we issue
users                  Entra subject -> internal user_id mapping
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret_hash TEXT,
    client_metadata TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    last_used_at INTEGER,
    register_ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_oauth_clients_last_used ON oauth_clients(last_used_at);

CREATE TABLE IF NOT EXISTS pending_authorizations (
    state TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    claude_redirect_uri TEXT NOT NULL,
    claude_state TEXT,
    code_challenge TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_auth_expires ON pending_authorizations(expires_at);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_codes_expires ON oauth_codes(expires_at);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at INTEGER,
    revoked_at INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    entra_sub TEXT NOT NULL,
    entra_tid TEXT NOT NULL,
    email TEXT,
    display_name TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(entra_sub, entra_tid)
);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key TEXT PRIMARY KEY,
    tokens REAL NOT NULL,
    last_refill_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_tokens (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    encrypted_blob BLOB NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _now_seconds() -> int:
    return int(time.time())


class Storage:
    """Thread-safe SQLite persistence layer.

    A single connection is shared across threads (sqlite3 is thread-safe in
    serialized mode); a lock serializes writes to avoid SQLITE_BUSY under
    contention. WAL mode lets readers proceed during writes.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Wrap a write in BEGIN/COMMIT under the lock. Reads can use
        `self._conn.execute` directly; SQLite handles read concurrency."""
        with self._lock:
            in_tx = self._conn.in_transaction
            if not in_tx:
                self._conn.execute("BEGIN")
            try:
                yield self._conn
                if not in_tx:
                    self._conn.execute("COMMIT")
            except Exception:
                if not in_tx and self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _init_schema(self) -> None:
        with self._lock:
            # `executescript` runs an implicit COMMIT before/after, so we
            # can't wrap it in our own transaction. CREATE TABLE IF NOT
            # EXISTS makes this idempotent for both fresh DBs and v1 → v2
            # upgrades (we only add tables; never alter existing columns).
            self._conn.executescript(_DDL)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] < SCHEMA_VERSION:
                # Forward-compat upgrade — DDL above already created any
                # missing tables, so just record the new version.
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                raise RuntimeError(
                    f"schema version too new: db={row['version']} code={SCHEMA_VERSION}"
                )

    # OAuth clients (DCR registry) -----------------------------------------

    def register_client(
        self,
        client_id: str,
        client_secret_hash: str | None,
        client_metadata: dict[str, Any],
        register_ip: str | None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO oauth_clients
                   (client_id, client_secret_hash, client_metadata,
                    registered_at, register_ip)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    client_id,
                    client_secret_hash,
                    json.dumps(client_metadata),
                    _now_seconds(),
                    register_ip,
                ),
            )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "client_secret_hash": row["client_secret_hash"],
            "client_metadata": json.loads(row["client_metadata"]),
            "registered_at": row["registered_at"],
            "last_used_at": row["last_used_at"],
            "register_ip": row["register_ip"],
        }

    def mark_client_used(self, client_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
                (_now_seconds(), client_id),
            )

    def count_clients(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]

    def cleanup_unused_clients(self, never_used_after: int, idle_after: int) -> int:
        """Delete clients that were registered but never exchanged a token,
        and clients that haven't been used in a long time. Returns the row
        count deleted."""
        now = _now_seconds()
        with self._tx() as conn:
            cur = conn.execute(
                """DELETE FROM oauth_clients
                   WHERE (last_used_at IS NULL AND registered_at < ?)
                      OR (last_used_at IS NOT NULL AND last_used_at < ?)""",
                (now - never_used_after, now - idle_after),
            )
            return cur.rowcount

    # Pending Entra authorizations -----------------------------------------

    def store_pending_authorization(
        self,
        state: str,
        client_id: str,
        claude_redirect_uri: str,
        claude_state: str | None,
        code_challenge: str,
        code_challenge_method: str,
        redirect_uri_provided_explicitly: bool,
        scopes: list[str],
        resource: str | None,
        ttl_seconds: int = 600,
    ) -> None:
        now = _now_seconds()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO pending_authorizations
                   (state, client_id, claude_redirect_uri, claude_state,
                    code_challenge, code_challenge_method,
                    redirect_uri_provided_explicitly, scopes, resource,
                    created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    state,
                    client_id,
                    claude_redirect_uri,
                    claude_state,
                    code_challenge,
                    code_challenge_method,
                    int(redirect_uri_provided_explicitly),
                    json.dumps(scopes),
                    resource,
                    now,
                    now + ttl_seconds,
                ),
            )

    def consume_pending_authorization(self, state: str) -> dict[str, Any] | None:
        """Atomically read-and-delete a pending authorization. Returns None if
        the state is unknown or expired."""
        now = _now_seconds()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM pending_authorizations WHERE state = ?", (state,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM pending_authorizations WHERE state = ?", (state,))
            if row["expires_at"] < now:
                return None
            return {
                "state": row["state"],
                "client_id": row["client_id"],
                "claude_redirect_uri": row["claude_redirect_uri"],
                "claude_state": row["claude_state"],
                "code_challenge": row["code_challenge"],
                "code_challenge_method": row["code_challenge_method"],
                "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
                "scopes": json.loads(row["scopes"]),
                "resource": row["resource"],
            }

    def cleanup_expired_pending(self) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM pending_authorizations WHERE expires_at < ?",
                (_now_seconds(),),
            )
            return cur.rowcount

    # OAuth codes (issued to Claude) ---------------------------------------

    def store_authorization_code(
        self,
        code: str,
        client_id: str,
        user_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        resource: str | None,
        ttl_seconds: int = 60,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO oauth_codes
                   (code, client_id, user_id, redirect_uri,
                    redirect_uri_provided_explicitly, code_challenge,
                    scopes, resource, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    client_id,
                    user_id,
                    redirect_uri,
                    int(redirect_uri_provided_explicitly),
                    code_challenge,
                    json.dumps(scopes),
                    resource,
                    _now_seconds() + ttl_seconds,
                ),
            )

    def load_authorization_code(self, code: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
        if row is None or row["expires_at"] < _now_seconds():
            return None
        return {
            "code": row["code"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "redirect_uri": row["redirect_uri"],
            "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
            "code_challenge": row["code_challenge"],
            "scopes": json.loads(row["scopes"]),
            "resource": row["resource"],
            "expires_at": row["expires_at"],
        }

    def consume_authorization_code(self, code: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
            if row["expires_at"] < _now_seconds():
                return None
            return {
                "code": row["code"],
                "client_id": row["client_id"],
                "user_id": row["user_id"],
                "redirect_uri": row["redirect_uri"],
                "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
                "code_challenge": row["code_challenge"],
                "scopes": json.loads(row["scopes"]),
                "resource": row["resource"],
                "expires_at": row["expires_at"],
            }

    # Refresh tokens -------------------------------------------------------

    def store_refresh_token(
        self,
        token_hash: str,
        client_id: str,
        user_id: str,
        scopes: list[str],
        expires_at: int | None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO refresh_tokens
                   (token_hash, client_id, user_id, scopes, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_hash, client_id, user_id, json.dumps(scopes), expires_at),
            )

    def load_refresh_token(self, token_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < _now_seconds():
            return None
        return {
            "token_hash": row["token_hash"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "scopes": json.loads(row["scopes"]),
            "expires_at": row["expires_at"],
        }

    def revoke_refresh_token(self, token_hash: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ?",
                (_now_seconds(), token_hash),
            )

    # Users (Entra mapping) ------------------------------------------------

    def get_or_create_user(
        self,
        user_id_factory,
        entra_sub: str,
        entra_tid: str,
        email: str | None,
        display_name: str | None,
    ) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE entra_sub = ? AND entra_tid = ?",
                (entra_sub, entra_tid),
            ).fetchone()
            if row is not None:
                return dict(row)
            user_id = user_id_factory()
            conn.execute(
                """INSERT INTO users
                   (user_id, entra_sub, entra_tid, email, display_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, entra_sub, entra_tid, email, display_name, _now_seconds()),
            )
            return {
                "user_id": user_id,
                "entra_sub": entra_sub,
                "entra_tid": entra_tid,
                "email": email,
                "display_name": display_name,
                "created_at": _now_seconds(),
            }

    # Garmin tokens (encrypted blob per user) ------------------------------

    def save_garmin_token(self, user_id: str, encrypted_blob: bytes) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO garmin_tokens(user_id, encrypted_blob, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     encrypted_blob = excluded.encrypted_blob,
                     updated_at = excluded.updated_at""",
                (user_id, encrypted_blob, _now_seconds()),
            )

    def load_garmin_token(self, user_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT encrypted_blob FROM garmin_tokens WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bytes(row["encrypted_blob"]) if row else None

    def delete_garmin_token(self, user_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM garmin_tokens WHERE user_id = ?", (user_id,))

    def has_garmin_token(self, user_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM garmin_tokens WHERE user_id = ?", (user_id,)
            ).fetchone()
            is not None
        )

    # Rate limit buckets (token bucket persistence) ------------------------

    def get_bucket(self, key: str) -> tuple[float, int] | None:
        row = self._conn.execute(
            "SELECT tokens, last_refill_ms FROM rate_limit_buckets WHERE key = ?",
            (key,),
        ).fetchone()
        return (row["tokens"], row["last_refill_ms"]) if row else None

    def upsert_bucket(self, key: str, tokens: float, last_refill_ms: int) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO rate_limit_buckets(key, tokens, last_refill_ms)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     tokens = excluded.tokens,
                     last_refill_ms = excluded.last_refill_ms""",
                (key, tokens, last_refill_ms),
            )
