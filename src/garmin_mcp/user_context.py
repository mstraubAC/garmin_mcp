"""Per-user Garmin client resolution.

Tools fetch the Garmin client for the current request via `get_garmin_client()`.
In stdio (single-user) mode the same client is returned for every call. The
HTTP transport (added in a later step) sets a ContextVar per request so that
the multi-user cache can return the right client.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from garminconnect import Garmin

DEFAULT_USER_ID = "default"

_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "garmin_mcp_current_user_id", default=None
)


class ClientCache:
    """Resolves a user_id to a Garmin client. Subclasses define lookup."""

    def get_or_load(self, user_id: str) -> Garmin:
        raise NotImplementedError


class SingleUserClientCache(ClientCache):
    """Stdio mode: one process, one Garmin client, ignored user_id."""

    def __init__(self, client: Garmin):
        self._client = client

    def get_or_load(self, user_id: str) -> Garmin:
        return self._client


_client_cache: Optional[ClientCache] = None


def set_client_cache(cache: ClientCache) -> None:
    global _client_cache
    _client_cache = cache


def current_user_id() -> str:
    return _current_user_id.get() or DEFAULT_USER_ID


def set_current_user_id(user_id: str):
    """Set the current user_id for this context (HTTP middleware uses this).

    Returns the token so callers can reset it via `_current_user_id.reset(token)`.
    """
    return _current_user_id.set(user_id)


def get_garmin_client() -> Garmin:
    if _client_cache is None:
        raise RuntimeError(
            "Garmin client cache not initialized. "
            "Call set_client_cache() during startup."
        )
    return _client_cache.get_or_load(current_user_id())
