"""Token-bucket rate limiting for OAuth proxy endpoints.

Persisted in SQLite so a process restart doesn't reset attacker buckets.
The bucket key is opaque to this module — callers pass strings like
"register:1.2.3.4" or "authorize:client-id".

Capacity-and-refill semantics:
  - capacity: max tokens
  - refill_per_second: float, tokens added per real-time second
  - cost: tokens debited per request

Usage:
    bucket = TokenBucket(storage, capacity=5, refill_per_second=5/3600)
    if not await bucket.try_consume("register:1.2.3.4"):
        return 429
"""

from __future__ import annotations

import asyncio
import time

from garmin_mcp.auth.storage import Storage


def _now_ms() -> int:
    return int(time.time() * 1000)


class TokenBucket:
    """Persistent token bucket. One instance per (capacity, refill_per_second)
    policy; the storage row is keyed by the caller-supplied string."""

    def __init__(self, storage: Storage, capacity: float, refill_per_second: float):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self._storage = storage
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)

    def _consume_sync(self, key: str, cost: float, now_ms: int) -> bool:
        existing = self._storage.get_bucket(key)
        if existing is None:
            tokens = self.capacity
            last_refill = now_ms
        else:
            tokens, last_refill = existing
            elapsed_seconds = max(0, (now_ms - last_refill) / 1000.0)
            tokens = min(self.capacity, tokens + elapsed_seconds * self.refill_per_second)
            last_refill = now_ms

        if tokens < cost:
            self._storage.upsert_bucket(key, tokens, last_refill)
            return False

        self._storage.upsert_bucket(key, tokens - cost, last_refill)
        return True

    async def try_consume(self, key: str, cost: float = 1.0) -> bool:
        """Returns True if the bucket had enough tokens (and they were debited).
        Returns False otherwise."""
        return await asyncio.to_thread(self._consume_sync, key, cost, _now_ms())

    def try_consume_sync(self, key: str, cost: float = 1.0) -> bool:
        """Sync variant for non-async call sites."""
        return self._consume_sync(key, cost, _now_ms())


class RegistrationGuard:
    """Layered defenses on the /register endpoint.

    Composes:
      - global cap on total registered clients
      - per-IP token bucket
      - optional shared bearer token (`MCP_REGISTRATION_TOKEN` env var)
      - HTTPS-or-localhost redirect URI hygiene
    """

    LOCALHOST_PREFIXES = (
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
    )

    def __init__(
        self,
        storage: Storage,
        per_ip_bucket: TokenBucket,
        global_cap: int = 10_000,
        shared_token: str | None = None,
        max_redirect_uris: int = 10,
        max_field_length: int = 1024,
    ):
        self._storage = storage
        self._per_ip = per_ip_bucket
        self._global_cap = global_cap
        self._shared_token = shared_token
        self._max_redirect_uris = max_redirect_uris
        self._max_field_length = max_field_length

    def check_shared_token(self, header_value: str | None) -> bool:
        """Returns True if the shared-token gate passes (or is disabled)."""
        if not self._shared_token:
            return True
        if not header_value or not header_value.startswith("Bearer "):
            return False
        return header_value[len("Bearer ") :].strip() == self._shared_token

    def check_redirect_uri(self, uri: str) -> bool:
        if not uri or len(uri) > self._max_field_length:
            return False
        if uri.startswith("https://"):
            return True
        return any(uri.startswith(prefix) for prefix in self.LOCALHOST_PREFIXES)

    def check_field_lengths(self, metadata: dict) -> bool:
        for v in metadata.values():
            if isinstance(v, str) and len(v) > self._max_field_length:
                return False
        return True

    def under_global_cap(self) -> bool:
        return self._storage.count_clients() < self._global_cap

    async def check_per_ip(self, ip: str) -> bool:
        return await self._per_ip.try_consume(f"register:{ip}")


class ToolCallGuard:
    """Per-user + global rate limiting on Garmin API tool calls.

    Two-layer token bucket:
      1. **Per-user** bucket — prevents one user from monopolizing the
         Garmin API quota (capacity=60, refill=60/60s ≈ 1 call/sec/user).
      2. **Global outbound** bucket — caps total calls to Garmin
         regardless of user count (capacity=120, refill=120/60s ≈ 2
         calls/sec total).

    Both layers must allow the call for it to proceed.  This keeps a
    single noisy user from tripping Garmin's IP-level limiter and
    affecting every other user on the same VPS.

    Usage (wired inside MultiUserClientCache):
        guard = ToolCallGuard(storage)
        if not await guard.try_consume(user_id):
            raise RateLimitExceededError(user_id)
    """

    def __init__(self, storage: Storage):
        self._per_user = TokenBucket(storage, capacity=60, refill_per_second=60 / 60)
        self._global = TokenBucket(storage, capacity=120, refill_per_second=120 / 60)

    async def try_consume(self, user_id: str) -> bool:
        """Returns True if both per-user and global buckets allow the call."""
        if not await self._per_user.try_consume(f"tool:{user_id}"):
            return False
        return await self._global.try_consume("tool:global")

    def try_consume_sync(self, user_id: str) -> bool:
        """Sync variant for test and non-async call sites."""
        if not self._per_user.try_consume_sync(f"tool:{user_id}"):
            return False
        return self._global.try_consume_sync("tool:global")


class RateLimitExceededError(Exception):
    """Raised when a tool call is blocked by the ToolCallGuard."""

    def __init__(self, user_id: str):
        super().__init__(f"Rate limit exceeded for user {user_id} — slow down and retry")
        self.user_id = user_id
