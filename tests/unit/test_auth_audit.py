"""Unit tests for the hash-chained audit log."""
import json
import time

import pytest

from garmin_mcp.auth.audit import AuditLog, _hash_line


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
    file_as_dir = tmp_path / "not-a-dir"
    file_as_dir.write_text("file")
    log = AuditLog(log_dir=file_as_dir / "subpath")
    log.record("e", client_id="c")


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    log = AuditLog(log_dir=tmp_path)

    def boom(*_, **__):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", boom)
    log.record("e")


def test_appends_across_calls(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    for i in range(5):
        log.record("e", n=i)
    lines = _read_lines(tmp_path)
    assert [l["n"] for l in lines] == [0, 1, 2, 3, 4]


# Hash chain ------------------------------------------------------------------


def test_each_line_has_prev_hash(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    log.record("e1")
    log.record("e2")
    lines = _read_lines(tmp_path)
    # First line: prev_hash is None (JSON null).
    assert lines[0]["prev_hash"] is None
    # Second line: prev_hash matches hash of first line.
    first_line_str = json.dumps(lines[0], separators=(",", ":"), ensure_ascii=False)
    assert lines[1]["prev_hash"] == _hash_line(first_line_str)


def test_chain_is_self_consistent(tmp_path):
    """Write 100 events, verify every prev_hash links correctly."""
    log = AuditLog(log_dir=tmp_path)
    for i in range(100):
        log.record("e", n=i)

    with open(sorted(tmp_path.glob("audit-*.log"))[0]) as f:
        raw_lines = [line.rstrip("\n") for line in f if line.strip()]

    prev = None
    for line_str in raw_lines:
        obj = json.loads(line_str)
        assert obj["prev_hash"] == prev, f"broken at n={obj.get('n')}"
        prev = _hash_line(line_str)


def test_restart_continues_chain(tmp_path):
    """After a restart (new AuditLog on the same file), chain continues."""
    log1 = AuditLog(log_dir=tmp_path)
    log1.record("e1")
    log1.record("e2")

    log2 = AuditLog(log_dir=tmp_path)
    log2.record("e3")

    lines = _read_lines(tmp_path)
    assert len(lines) == 3
    # The chain should be unbroken across the restart.
    prev = None
    for line_obj in lines:
        assert line_obj["prev_hash"] == prev
        prev = _hash_line(json.dumps(line_obj, separators=(",", ":"), ensure_ascii=False))


# verify_audit CLI ------------------------------------------------------------


from garmin_mcp.maintenance.verify_audit import verify as verify_audit


def test_verify_intact_chain_passes(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    for i in range(10):
        log.record("e", n=i)
    files = sorted(tmp_path.glob("audit-*.log"))
    assert verify_audit(files[0]) == 0  # no errors


def test_verify_broken_chain_detects_tamper(tmp_path):
    log = AuditLog(log_dir=tmp_path)
    for i in range(5):
        log.record("e", n=i)

    files = sorted(tmp_path.glob("audit-*.log"))
    # Corrupt line 3 (change a character).
    lines = files[0].read_text().splitlines(True)
    lines[2] = lines[2].replace('"n":2', '"n":999')
    files[0].write_text("".join(lines))

    assert verify_audit(files[0]) == 1  # one error
