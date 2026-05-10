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
import logging

from garmin_mcp.auth.storage import Storage

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600  # 1 hour
DEFAULT_NEVER_USED_AFTER = 24 * 3600  # 1 day
DEFAULT_IDLE_AFTER = 90 * 24 * 3600  # 90 days


async def cleanup_loop(
    storage: Storage,
    onboarding_manager=None,
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
            await tick_once(storage, onboarding_manager, never_used_after, idle_after)
        except Exception:  # pragma: no cover — defense against unknown failures
            log.exception("cleanup tick failed")


async def tick_once(
    storage: Storage,
    onboarding_manager=None,
    never_used_after: int = DEFAULT_NEVER_USED_AFTER,
    idle_after: int = DEFAULT_IDLE_AFTER,
) -> dict[str, int]:
    """Run one cleanup pass. Returns counts for tests / logging."""
    n_clients = await asyncio.to_thread(
        storage.cleanup_unused_clients,
        never_used_after=never_used_after,
        idle_after=idle_after,
    )
    n_pending = await asyncio.to_thread(storage.cleanup_expired_pending)
    if n_clients or n_pending:
        log.info(
            "cleanup: removed %d unused clients, %d expired pending auths",
            n_clients,
            n_pending,
        )
    n_sessions = onboarding_manager.evict_terminal_sessions() if onboarding_manager else 0
    return {"clients": n_clients, "pending": n_pending, "sessions": n_sessions}
