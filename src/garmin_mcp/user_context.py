"""Per-user Garmin client resolution.

Tools fetch the Garmin client for the current request via `get_garmin_client()`.
The cache strategy depends on the deployment mode:

* `SingleUserClientCache` — stdio mode and the legacy no-auth HTTP test path.
  One Garmin client, returned for every user_id.
* `MultiUserClientCache` — production HTTP mode. Loads each user's
  Fernet-encrypted Garmin tokens on demand, builds a `Garmin` instance, and
  caches it in memory with an idle TTL. If a user has never onboarded,
  `get_or_load` raises `UserNotOnboardedError` so the request can be turned
  into a clear "go visit /onboard" message instead of a stack trace.
* `RateLimitedGarminProxy` — wraps a Garmin instance with per-user +
  global rate limiting (TokenBucket from `auth/throttle.py`). Every method
  call is checked before hitting the real Garmin API.
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Callable, Optional

from garminconnect import Garmin

if TYPE_CHECKING:
    from garmin_mcp.auth.garmin_tokens import GarminTokenStore
    from garmin_mcp.auth.throttle import ToolCallGuard

DEFAULT_USER_ID = "default"

_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "garmin_mcp_current_user_id", default=None
)


class UserNotOnboardedError(Exception):
    """Raised when an authenticated user has no Garmin tokens stored.

    The HTTP layer should turn this into a 401-or-similar response that
    points the user at /onboard instead of letting tools fail with a
    confusing AttributeError.
    """

    def __init__(self, user_id: str):
        super().__init__(
            f"user {user_id} has no Garmin credentials stored — visit /onboard"
        )
        self.user_id = user_id


class ClientCache:
    """Resolves a user_id to a Garmin client. Subclasses define lookup."""

    def get_or_load(self, user_id: str) -> Garmin:
        raise NotImplementedError

    def invalidate(self, user_id: str) -> None:
        """Drop any cached client for `user_id` (no-op if not cached)."""


class SingleUserClientCache(ClientCache):
    """Stdio mode and the legacy no-auth HTTP test path: one process, one
    Garmin client, ignored user_id."""

    def __init__(self, client: Garmin):
        self._client = client

    def get_or_load(self, user_id: str) -> Garmin:
        return self._client


class MultiUserClientCache(ClientCache):
    """Per-user lazy-load + idle-TTL cache backed by `GarminTokenStore`.

    The cache holds a fully-initialized `Garmin` instance per user_id.
    Entries are evicted after `idle_ttl_seconds` of no access. This bounds
    memory while still amortizing the cost of `garth.loads()` across the
    many tool calls a single Claude session makes.

    If a `ToolCallGuard` is provided, every returned client is wrapped in a
    `RateLimitedGarminProxy` that checks per-user and global rate limits
    before forwarding calls to the real Garmin API.
    """

    def __init__(
        self,
        token_store: "GarminTokenStore",
        garmin_factory: Callable[[], Garmin] | None = None,
        idle_ttl_seconds: int = 30 * 60,
        is_cn: bool = False,
        tool_call_guard: "ToolCallGuard | None" = None,
    ):
        self._tokens = token_store
        self._idle_ttl = idle_ttl_seconds
        self._is_cn = is_cn
        # Allow tests to inject a fake Garmin constructor.
        self._garmin_factory = garmin_factory or (lambda: Garmin(is_cn=is_cn))
        self._entries: dict[str, tuple[Garmin, float]] = {}
        self._lock = threading.Lock()
        self._guard = tool_call_guard

    def get_or_load(self, user_id: str) -> Garmin:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                client, last_used = entry
                if now - last_used < self._idle_ttl:
                    self._entries[user_id] = (client, now)
                    return client
                # Expired — drop and reload below.
                del self._entries[user_id]

        token_blob = self._tokens.load(user_id)
        if token_blob is None:
            raise UserNotOnboardedError(user_id)

        client = self._garmin_factory()
        client.garth.loads(token_blob)

        # Wrap with rate limiting if a guard is configured.
        if self._guard is not None:
            client = RateLimitedGarminProxy(client, user_id, self._guard)

        with self._lock:
            self._entries[user_id] = (client, time.monotonic())
        return client

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._entries.pop(user_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


class RateLimitedGarminProxy:
    """Wraps a Garmin instance, checking ToolCallGuard before each method call.

    Every attribute access that returns a callable is wrapped so that
    `guard.try_consume(user_id)` is called first.  If the guard denies
    the call, `RateLimitExceededError` is raised before the real Garmin
    API is touched.

    Non-callable attributes (e.g. `garth`) pass through directly.
    """

    def __init__(self, client: Garmin, user_id: str, guard: "ToolCallGuard"):
        self._client = client
        self._user_id = user_id
        self._guard = guard

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _rate_limited(*args, **kwargs):
            from garmin_mcp.auth.throttle import RateLimitExceededError

            # Try sync path first (works in tests and non-async call sites);
            # fall back to async if an event loop is already running.
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — use sync variant.
                if not self._guard.try_consume_sync(self._user_id):
                    raise RateLimitExceededError(self._user_id)
                return attr(*args, **kwargs)

            # Running loop — use async variant via run_coroutine_threadsafe
            # if we're on a different thread, or schedule directly.
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                self._guard.try_consume(self._user_id), loop
            )
            if not future.result():
                raise RateLimitExceededError(self._user_id)
            return attr(*args, **kwargs)

        return _rate_limited


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
