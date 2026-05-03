"""Structured audit log for OAuth proxy events.

One JSON object per line, one file per day. Default log dir comes from the
`GARMIN_MCP_AUDIT_DIR` env var (or /var/log/garmin-mcp). Failures to write
the log NEVER raise — auditing is best-effort and must not break a real
request.

Events recorded (one per call site):
    register.success      DCR succeeded
    register.rejected     DCR refused (reason in `detail`)
    authorize.start       /authorize hit, redirecting to Entra
    authorize.callback    Entra returned to /callback
    token.issued          /token returned an access token
    token.refused         /token rejected (reason in `detail`)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only structured log. One file per UTC date."""

    def __init__(self, log_dir: str | Path | None = None):
        log_dir = log_dir or os.environ.get(
            "GARMIN_MCP_AUDIT_DIR", "/var/log/garmin-mcp"
        )
        self._log_dir = Path(log_dir)
        self._lock = threading.Lock()
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # If we can't create the dir, fall back to stderr-only logging.
            logging.getLogger(__name__).warning(
                "audit log dir %s not writable: %s — auditing disabled", log_dir, e
            )
            self._log_dir = None  # type: ignore[assignment]

    def _path_for_today(self) -> Path | None:
        if self._log_dir is None:
            return None
        return self._log_dir / f"audit-{time.strftime('%Y-%m-%d', time.gmtime())}.log"

    def record(self, event: str, **fields: Any) -> None:
        line_obj: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
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
        except OSError as e:
            # Don't let a full disk break OAuth.
            logging.getLogger(__name__).warning("audit write failed: %s", e)
