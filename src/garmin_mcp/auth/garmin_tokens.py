"""Per-user Garmin OAuth-token storage, encrypted at rest with Fernet.

The plaintext is whatever `garth.dumps()` returns for a logged-in session
(a string). We encrypt that string and persist the bytes; on load we
decrypt and hand the string back so callers can pass it straight to
`garth.loads()` (or `garmin.garth.loads()` on a fresh `Garmin` instance).

The Fernet key comes from the `GARMIN_MCP_DATA_KEY` env var. Generate one
with `cryptography.fernet.Fernet.generate_key().decode()`.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from garmin_mcp.auth.storage import Storage


class GarminTokenStore:
    """Encryption wrapper around `Storage.{save,load,delete}_garmin_token`."""

    def __init__(self, storage: Storage, fernet_key: str | bytes):
        if not fernet_key:
            raise ValueError("fernet_key is required (set GARMIN_MCP_DATA_KEY)")
        if isinstance(fernet_key, str):
            fernet_key = fernet_key.encode("ascii")
        # Fernet validates the key shape during __init__; surface a clear error.
        try:
            self._fernet = Fernet(fernet_key)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid Fernet key (must be 32 url-safe base64 bytes): {e}") from e
        self._storage = storage

    def save(self, user_id: str, garth_dump: str) -> None:
        token_bytes = self._fernet.encrypt(garth_dump.encode("utf-8"))
        self._storage.save_garmin_token(user_id, token_bytes)

    def load(self, user_id: str) -> str | None:
        blob = self._storage.load_garmin_token(user_id)
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob).decode("utf-8")
        except InvalidToken as e:
            raise ValueError(
                f"failed to decrypt Garmin token for {user_id}; "
                "GARMIN_MCP_DATA_KEY may have been rotated"
            ) from e

    def delete(self, user_id: str) -> None:
        self._storage.delete_garmin_token(user_id)

    def has(self, user_id: str) -> bool:
        return self._storage.has_garmin_token(user_id)
