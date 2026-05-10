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
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from garminconnect import Garmin

if TYPE_CHECKING:
    from garmin_mcp.auth.garmin_tokens import GarminTokenStore
    from garmin_mcp.auth.throttle import ToolCallGuard

DEFAULT_USER_ID = "default"

# ContextVar for client IP captured on POST /register.
register_ip: ContextVar[str] = ContextVar("garmin_mcp_register_ip", default="")
# Set by /callback route; read by OAuth provider for ticket binding (H23).
onboard_ip: ContextVar[str] = ContextVar("onboard_ip", default="")
onboard_ua_hash: ContextVar[str] = ContextVar("onboard_ua_hash", default="")

_current_user_id: ContextVar[str | None] = ContextVar("garmin_mcp_current_user_id", default=None)


class UserNotOnboardedError(Exception):
    """Raised when an authenticated user has no Garmin tokens stored.

    The HTTP layer should turn this into a 401-or-similar response that
    points the user at /onboard instead of letting tools fail with a
    confusing AttributeError.
    """

    def __init__(self, user_id: str):
        super().__init__(f"user {user_id} has no Garmin credentials stored — visit /onboard")
        self.user_id = user_id


class GarminSessionExpiredError(Exception):
    """Raised when a Garmin API call returns 401 (token expired).

    The HTTP layer should turn this into a structured JSON-RPC error
    containing the onboarding URL so the user can re-authenticate.
    """

    def __init__(self, user_id: str, onboarding_url: str = ""):
        super().__init__(
            f"Garmin session expired for user {user_id} — re-onboard at {onboarding_url}"
        )
        self.user_id = user_id
        self.onboarding_url = onboarding_url


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
        token_store: GarminTokenStore,
        garmin_factory: Callable[[], Garmin] | None = None,
        idle_ttl_seconds: int = 30 * 60,
        is_cn: bool = False,
        tool_call_guard: ToolCallGuard | None = None,
        onboarding_url: str = "",
    ):
        self._tokens = token_store
        self._idle_ttl = idle_ttl_seconds
        self._is_cn = is_cn
        self._onboarding_url = onboarding_url
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

        # Double-check: another thread may have loaded + cached this entry
        # while we were blocking on decrypting the token blob.
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                client_cached, last_used = entry
                if now - last_used < self._idle_ttl:
                    return client_cached

        client = self._garmin_factory()
        client.garth.loads(token_blob)

        # Wrap with rate limiting + session-expiry detection.
        if self._guard is not None:
            client = RateLimitedGarminProxy(
                client,
                user_id,
                self._guard,
                cache=self,
                onboarding_url=self._onboarding_url,
            )

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
    """Wraps a Garmin instance, checking ToolCallGuard before each method call
    and catching 401 responses to detect expired sessions.

    Every attribute access that returns a callable is wrapped so that:
      1. `guard.try_consume(user_id)` is called first — if denied,
         `RateLimitExceededError` is raised.
      2. If the real Garmin API returns a 401 (GarminConnectAuthenticationError),
         the cache entry is invalidated and `GarminSessionExpiredError` is raised
         with the onboarding URL.

    Non-callable attributes (e.g. `garth`) pass through directly.
    """

    def __init__(
        self,
        client: Garmin,
        user_id: str,
        guard: ToolCallGuard,
        cache: ClientCache,
        onboarding_url: str = "",
    ):
        self._client = client
        self._user_id = user_id
        self._guard = guard
        self._cache = cache
        self._onboarding_url = onboarding_url

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _guarded(*args, **kwargs):
            # Rate-limit check.
            import asyncio

            from garmin_mcp.auth.throttle import RateLimitExceededError

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                if not self._guard.try_consume_sync(self._user_id):
                    raise RateLimitExceededError(self._user_id)
            else:
                future = asyncio.run_coroutine_threadsafe(
                    self._guard.try_consume(self._user_id), loop
                )
                if not future.result():
                    raise RateLimitExceededError(self._user_id)

            # Call the real Garmin API; catch 401 → session expired.
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                if _is_garmin_401(exc):
                    self._cache.invalidate(self._user_id)
                    raise GarminSessionExpiredError(self._user_id, self._onboarding_url) from exc
                raise

        return _guarded


def _is_garmin_401(exc: Exception) -> bool:
    """Returns True if the exception indicates a 401 from Garmin."""
    # GarminConnectAuthenticationError: the garminconnect library raises
    # this for auth failures. Also check for generic HTTP 401.
    try:
        from garminconnect import GarminConnectAuthenticationError
    except ImportError:
        GarminConnectAuthenticationError = None  # type: ignore[assignment]

    if GarminConnectAuthenticationError is not None and isinstance(
        exc, GarminConnectAuthenticationError
    ):
        return True

    # garth wraps HTTP errors; check for 401 in any chained exception.
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 401:
        return True

    # Check __cause__ chain for garth.exc.GarthHTTPError with 401.
    cause = exc.__cause__
    while cause is not None:
        cause_status = getattr(cause, "status", None) or getattr(cause, "status_code", None)
        if cause_status == 401:
            return True
        # Also check if cause is GarminConnectAuthenticationError
        if GarminConnectAuthenticationError is not None and isinstance(
            cause, GarminConnectAuthenticationError
        ):
            return True
        cause = cause.__cause__

    return False


_client_cache: ClientCache | None = None


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
            "Garmin client cache not initialized. Call set_client_cache() during startup."
        )
    return _client_cache.get_or_load(current_user_id())
