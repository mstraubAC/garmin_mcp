"""Unit tests for the audit log."""
import json
import time

import pytest

from garmin_mcp.auth.audit import AuditLog


def _read_lines(log_dir):
    files = sorted(log_dir.glob("audit-*.log"))
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def test_record_writes_one_json_object_per_call(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    log.record("register.success", client_id="c1", ip="1.2.3.4")
    log.record("token.issued", client_id="c1", user_id="u1")
    lines = _read_lines(tmp_path)
    assert len(lines) == 2
    assert lines[0]["event"] == "register.success"
    assert lines[0]["client_id"] == "c1"
    assert lines[0]["ip"] == "1.2.3.4"
    assert "ts" in lines[0]


def test_none_fields_are_dropped(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    log.record("e", client_id="c", missing=None)
    line = _read_lines(tmp_path)[0]
    assert line["client_id"] == "c"
    assert "missing" not in line


def test_unwritable_dir_does_not_raise(tmp_path):
    # Pass a path we can't create (a file masquerading as a dir).
    file_as_dir = tmp_path / "not-a-dir"
    file_as_dir.write_text("file")
    log = AuditLog(log_dir=file_as_dir / "subpath")
    # Must not raise.
    log.record("e", client_id="c")


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    log = AuditLog(log_dir=tmp_path)

    def boom(*_, **__):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", boom)
    log.record("e")  # must not raise


def test_appends_across_calls(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    for i in range(5):
        log.record("e", n=i)
    lines = _read_lines(tmp_path)
    assert [l["n"] for l in lines] == [0, 1, 2, 3, 4]
