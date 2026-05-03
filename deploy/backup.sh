#!/usr/bin/env bash
# Snapshot the Garmin MCP state volume to a tarball.
#
# Usage:
#   ./backup.sh                       # writes to /var/backups/garmin-mcp/
#   ./backup.sh /path/to/output       # custom dir
#
# Add to root crontab for nightly backups:
#   0 3 * * * /opt/garmin-mcp/deploy/backup.sh >> /var/log/garmin-mcp-backup.log 2>&1
#
# A real off-site backup story (restic, rclone, S3, …) lives in step 9
# of the rollout plan; this script only takes a local snapshot.

set -euo pipefail

OUT_DIR="${1:-/var/backups/garmin-mcp}"
COMPOSE_DIR="$(cd "$(dirname "$0")" && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "${OUT_DIR}"

# Snapshot the SQLite DB while the server is running using SQLite's online
# backup API (consistent point-in-time copy, doesn't block writers).
echo "Snapshotting SQLite to ${OUT_DIR}/state-${TS}.db..." >&2
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" exec -T garmin-mcp \
    python -c "
import sqlite3
src = sqlite3.connect('/var/lib/garmin-mcp/state.db')
dst = sqlite3.connect('/tmp/state-${TS}.db')
src.backup(dst)
dst.close(); src.close()
"
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" cp \
    "garmin-mcp:/tmp/state-${TS}.db" "${OUT_DIR}/state-${TS}.db"
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" exec -T garmin-mcp \
    rm -f "/tmp/state-${TS}.db"

# Audit log isn't critical, but include it for forensics.
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" cp \
    garmin-mcp:/var/log/garmin-mcp "${OUT_DIR}/audit-${TS}/" || true

# Keep only the 30 most recent SQLite snapshots.
ls -1t "${OUT_DIR}"/state-*.db 2>/dev/null | tail -n +31 | xargs -r rm -f
ls -1td "${OUT_DIR}"/audit-* 2>/dev/null | tail -n +31 | xargs -r rm -rf

echo "Backup complete: ${OUT_DIR}/state-${TS}.db" >&2
