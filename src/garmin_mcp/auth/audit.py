"""Structured audit log for OAuth proxy events.

One JSON object per line, one file per day. Default log dir comes from the
`GARMIN_MCP_AUDIT_DIR` env var (or /var/log/garmin-mcp). Failures to write
the log NEVER raise — auditing is best-effort and must not break a real
request.

Each line includes a `prev_hash` field (SHA-256 of the previous line's
full JSON), forming a tamper-evident chain. The chain resets per day
(file boundary). A separate `garmin-mcp-verify-audit` CLI walks a file
and reports any broken links.

Events recorded (one per call site):
    register.success      DCR succeeded
    register.rejected     DCR refused (reason in `detail`)
    authorize.start       /authorize hit, redirecting to Entra
    authorize.callback    Entra returned to /callback
    token.issued          /token returned an access token
    token.refused         /token rejected (reason in `detail`)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only, hash-chained structured log. One file per UTC date.

    The chain resets per file (per day). On init, reads the last line
    of today's file to continue the chain; if the file is empty or
    doesn't exist, the chain starts fresh.
    """

    def __init__(self, log_dir: str | Path | None = None):
        log_dir = log_dir or os.environ.get("GARMIN_MCP_AUDIT_DIR", "/var/log/garmin-mcp")
        self._log_dir = Path(log_dir)
        self._lock = threading.Lock()
        self._prev_hash: str | None = None
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.getLogger(__name__).warning(
                "audit log dir %s not writable: %s — auditing disabled", log_dir, e
            )
            self._log_dir = None  # type: ignore[assignment]
            return

        # Read last line of today's file to continue the chain.
        path = self._path_for_today()
        if path is not None and path.exists():
            try:
                last = _read_last_line(path)
                if last:
                    self._prev_hash = _hash_line(last)
            except (OSError, json.JSONDecodeError, ValueError):
                pass  # Corrupted last line? Start fresh.

    def _path_for_today(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / f"audit-{time.strftime('%Y-%m-%d', time.gmtime())}.log"

    def record(self, event: str, **fields: Any) -> None:
        line_obj: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "prev_hash": self._prev_hash,
        }
        for k, v in fields.items():
            if v is not None:
                line_obj[k] = v
        line = json.dumps(line_obj, separators=(",", ":"), ensure_ascii=False)

        path = self._path_for_today()
        if path is None:
            return
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                # Update chain head for the next call.
                self._prev_hash = _hash_line(line)
        except OSError as e:
            logging.getLogger(__name__).warning("audit write failed: %s", e)


def _hash_line(line: str) -> str:
    """SHA-256 of a single log line (the full JSON string, excluding the
    trailing newline)."""
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


def _read_last_line(path: Path) -> str | None:
    """Read the last non-empty line of a file efficiently.

    Returns None if the file is empty or unreadable."""
    with path.open("rb") as f:
        # Seek to last ~4KB (more than enough for one JSON log line).
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None
        chunk_size = min(size, 4096)
        f.seek(size - chunk_size)
        chunk = f.read(chunk_size).decode("utf-8", errors="replace")
        lines = chunk.strip().split("\n")
        return lines[-1] if lines else None
