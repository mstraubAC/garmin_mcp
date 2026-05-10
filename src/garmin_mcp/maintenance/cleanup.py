"""Periodic SQLite cleanup task for the OAuth proxy.

Runs in-process as an asyncio background task started by the lifespan
(`make_app(background_task_factories=[...])`). One tick per
`interval_seconds`:

  * Drop expired pending Entra authorizations (state tokens older than ~10 min)
  * Drop oauth_clients rows that registered but never exchanged a token
    after `never_used_after` seconds (default 24 h)
  * Drop oauth_clients rows that haven't been used in `idle_after` seconds
    (default 90 d)

Failures are logged but never raise — keeping the cleanup tick resilient
matters more than catching every error.
"""

from __future__ import annotations

import asyncio
from typing import Any
import logging

from garmin_mcp.auth.storage import Storage

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600  # 1 hour
DEFAULT_NEVER_USED_AFTER = 24 * 3600  # 1 day
DEFAULT_IDLE_AFTER = 90 * 24 * 3600  # 90 days


async def cleanup_loop(
    storage: Storage,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    never_used_after: int = DEFAULT_NEVER_USED_AFTER,
    idle_after: int = DEFAULT_IDLE_AFTER,
) -> None:
    """Repeat `tick_once()` every `interval_seconds`. Cancellable."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        try:
            await tick_once(storage, never_used_after, idle_after)
        except Exception:  # pragma: no cover — defense against unknown failures
            log.exception("cleanup tick failed")


async def tick_once(
    storage: Storage,
    onboarding_manager: Any | None = None,
    never_used_after: int = DEFAULT_NEVER_USED_AFTER,
    idle_after: int = DEFAULT_IDLE_AFTER,
) -> dict[str, int]:
    """Run one cleanup pass — SQLite housekeeping + onboarding session eviction."""
    n_clients = 0
    n_pending = 0
    with storage._conn:
        n_clients = storage._conn.execute(
            "DELETE FROM oauth_clients WHERE "
            "(registered_at < ? AND last_used_at IS NULL) OR "
            "(last_used_at IS NOT NULL AND last_used_at < ?)",
            (int(time.time()) - never_used_after, int(time.time()) - idle_after),
        ).rowcount
        n_pending = storage._conn.execute(
            "DELETE FROM pending_authorizations WHERE expires_at < ?",
            (int(time.time()),),
        ).rowcount
        storage._conn.commit()
    n_sessions = 0
    if onboarding_manager is not None:
        n_sessions = onboarding_manager.evict_terminal_sessions()
    return {"clients": n_clients, "pending": n_pending, "sessions": n_sessions}