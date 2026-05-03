"""Unit tests for the audit alert anomaly detector."""

import json
import time

from garmin_mcp.maintenance.audit_alert import _parse_iso, alert_once


def _write_audit(path, events):
    """Write a list of {'ts': ..., 'event': ...} dicts as an audit file."""
    lines = []
    for ev in events:
        lines.append(json.dumps(ev, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n")


def test_alert_once_no_file_does_nothing(tmp_path):
    """No audit file → no alert."""
    # Should not raise.
    import asyncio

    asyncio.run(alert_once(str(tmp_path)))


def test_alert_once_below_threshold_does_not_warn(tmp_path, caplog):
    """Below threshold → no warning."""
    now = time.time()
    ts = time.strftime("%Y-%m-%d", time.gmtime(now))
    audit_file = tmp_path / f"audit-{ts}.log"
    _write_audit(
        audit_file,
        [
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30)),
                "event": "register.success",
            },
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 20)),
                "event": "register.success",
            },
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                "event": "register.success",
            },
        ],
    )

    import asyncio

    asyncio.run(alert_once(str(tmp_path), register_threshold=10, window_minutes=5))
    assert "AUDIT_ALERT" not in caplog.text


def test_alert_once_above_threshold_warns(tmp_path, caplog):
    """Above threshold → WARNING logged."""
    now = time.time()
    ts = time.strftime("%Y-%m-%d", time.gmtime(now))
    audit_file = tmp_path / f"audit-{ts}.log"
    events = []
    for i in range(15):
        events.append(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 10)),
                "event": "register.success",
            }
        )
    _write_audit(audit_file, events)

    import asyncio

    asyncio.run(alert_once(str(tmp_path), register_threshold=10, window_minutes=5))
    assert "AUDIT_ALERT" in caplog.text


def test_alert_once_ignores_other_events(tmp_path, caplog):
    """Non-register.success events don't count toward threshold."""
    now = time.time()
    ts = time.strftime("%Y-%m-%d", time.gmtime(now))
    audit_file = tmp_path / f"audit-{ts}.log"
    _write_audit(
        audit_file,
        [
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "event": "token.issued",
            },
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "event": "token.issued",
            },
        ]
        * 20,
    )

    import asyncio

    asyncio.run(alert_once(str(tmp_path), register_threshold=1, window_minutes=5))
    assert "AUDIT_ALERT" not in caplog.text


def test_parse_iso_utc():
    ts = _parse_iso("2026-05-03T14:30:00Z")
    assert ts is not None
    assert ts > 0


def test_parse_iso_invalid():
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None
