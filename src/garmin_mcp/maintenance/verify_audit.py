"""Verify the hash chain of an audit log file.

Usage:
    garmin-mcp-verify-audit /var/log/garmin-mcp/audit-2026-05-03.log

Each line in the file must contain a `prev_hash` that matches the SHA-256
of the immediately preceding line. The chain resets per file (per day), so
the first line's `prev_hash` should be `null`.

Exit code 0 if the chain is intact; exit code 1 with a message if a broken
link is found.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


def verify(path: Path) -> int:
    """Verify the hash chain in `path`. Returns the number of errors found."""
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    errors = 0
    prev_hash: str | None = None

    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"line {lineno}: not valid JSON: {e}", file=sys.stderr)
                errors += 1
                continue

            claimed = obj.get("prev_hash")
            if claimed != prev_hash:
                print(
                    f"line {lineno}: prev_hash mismatch — "
                    f"expected {prev_hash!r}, got {claimed!r}",
                    file=sys.stderr,
                )
                errors += 1

            prev_hash = _hash_line(line)

    if errors == 0:
        print(f"ok: {path} — hash chain intact ({lineno} lines)")
    else:
        print(f"{errors} error(s) in {path}")
    return errors


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: garmin-mcp-verify-audit <audit-file>", file=sys.stderr)
        sys.exit(2)

    errors = verify(Path(sys.argv[1]))
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
