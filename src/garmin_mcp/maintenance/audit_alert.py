"""Periodic audit-log anomaly detection.

Runs as an in-process asyncio background task. Reads today's audit log
and counts events per type; if `register.success` rate exceeds a threshold
over a sliding window, logs a WARNING to stderr (routable via
`docker logs --follow | grep`).

Alerting is log-based — operators wire routing to email/Slack/push via
their own infrastructure. This module only produces the signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_AUDIT_DIR = "/var/log/garmin-mcp"
DEFAULT_INTERVAL_SECONDS = 60
# Alert if >10 register.success events in a 5-minute window.
REGISTER_RATE_THRESHOLD = 10
REGISTER_WINDOW_MINUTES = 5


async def audit_alert_loop(
    audit_dir: str = DEFAULT_AUDIT_DIR,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    register_threshold: int = REGISTER_RATE_THRESHOLD,
    window_minutes: int = REGISTER_WINDOW_MINUTES,
) -> None:
    """Repeat `alert_once()` every `interval_seconds`. Cancellable."""
    while True:
        try:
            await alert_once(
                audit_dir,
                register_threshold=register_threshold,
                window_minutes=window_minutes,
            )
        except Exception:
            log.exception("audit alert tick failed")
        await asyncio.sleep(interval_seconds)


async def alert_once(
    audit_dir: str,
    register_threshold: int = REGISTER_RATE_THRESHOLD,
    window_minutes: int = REGISTER_WINDOW_MINUTES,
) -> None:
    """Read today's audit log and log a warning if the register.success
    rate exceeds `register_threshold` events in the last `window_minutes`."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    audit_path = Path(audit_dir) / f"audit-{today}.log"

    if not audit_path.exists():
        return

    cutoff = time.time() - (window_minutes * 60)
    count = 0

    try:
        # Read last ~100 lines (enough for the window at any reasonable rate).
        lines = await _read_tail(audit_path, 100)
        for line in lines:
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("event") != "register.success":
                continue
            # Parse ISO timestamp to compare against the window.
            ts = _parse_iso(obj.get("ts", ""))
            if ts is not None and ts >= cutoff:
                count += 1
    except OSError:
        return

    if count > register_threshold:
        log.warning(
            "AUDIT_ALERT: register.success rate exceeded — "
            "%d events in the last %d minutes (threshold=%d)",
            count,
            window_minutes,
            register_threshold,
        )


def _parse_iso(ts: str) -> float | None:
    """Parse an ISO 8601 UTC timestamp like '2026-05-03T14:30:00Z'
    into a Unix timestamp. Returns None on failure."""
    try:
        import calendar

        st = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return calendar.timegm(st)
    except (ValueError, OverflowError):
        return None


async def _read_tail(path: Path, max_lines: int) -> list[str]:
    """Read the last `max_lines` lines of a file (async, via to_thread)."""
    return await asyncio.to_thread(_read_tail_sync, path, max_lines)


def _read_tail_sync(path: Path, max_lines: int) -> list[str]:
    """Read last N lines efficiently without loading the whole file."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return []

        # Read the last 8KB (more than enough for max_lines audit log lines).
        chunk_size = min(size, 8192)
        f.seek(size - chunk_size)
        chunk = f.read(chunk_size).decode("utf-8", errors="replace")
        lines = chunk.split("\n")

        # If there are fewer lines than requested, return all.
        if len(lines) <= max_lines + 1:
            return [line for line in lines if line]

        return lines[-(max_lines + 1) : -1]
