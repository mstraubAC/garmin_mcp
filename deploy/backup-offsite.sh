#!/usr/bin/env bash
# Off-site backup: snapshot SQLite, then ship to S3-compatible storage via restic.
#
# Prerequisites:
#   - restic installed (https://restic.net)
#   - /etc/garmin-mcp/backup.env (mode 0600) with the following vars:
#       RESTIC_REPOSITORY   e.g. s3:s3.eu-central-1.amazonaws.com/my-bucket/garmin-mcp
#       RESTIC_PASSWORD     restic repository encryption key
#       AWS_ACCESS_KEY_ID   S3 credentials
#       AWS_SECRET_ACCESS_KEY
#
# Usage (add to root crontab):
#   0 3 * * * /opt/garmin-mcp/deploy/backup-offsite.sh >> /var/log/garmin-mcp-backup.log 2>&1
#   # Weekly data integrity check (Sunday at 4am):
#   0 4 * * 0 /opt/garmin-mcp/deploy/backup-offsite.sh --check >> /var/log/garmin-mcp-backup.log 2>&1
#
# Recovery:
#   restic -r "$RESTIC_REPOSITORY" restore latest --target /restore
#   docker compose -f /opt/garmin-mcp/deploy/docker-compose.yml cp \
#       /restore/state-*.db garmin-mcp:/var/lib/garmin-mcp/state.db

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_ENV="/etc/garmin-mcp/backup.env"

if [[ ! -f "${BACKUP_ENV}" ]]; then
    echo "error: ${BACKUP_ENV} not found — see deploy/backup.env.example" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "${BACKUP_ENV}"

if [[ -z "${RESTIC_REPOSITORY:-}" || -z "${RESTIC_PASSWORD:-}" ]]; then
    echo "error: RESTIC_REPOSITORY and RESTIC_PASSWORD must be set in ${BACKUP_ENV}" >&2
    exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TEMP_DIR}"' EXIT

# 1. Local snapshot (reuses existing backup.sh).
echo "[${TS}] Starting local snapshot..." >&2
"${SCRIPT_DIR}/backup.sh" "${TEMP_DIR}"

# 2. Ship to off-site storage.
echo "[${TS}] Uploading to ${RESTIC_REPOSITORY}..." >&2
restic backup "${TEMP_DIR}" --tag "auto-${TS}" --host garmin-mcp 2>&1

# 3. Retention: keep 7 daily, 4 weekly, 6 monthly snapshots.
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>&1

# 4. Weekly data integrity check (Sundays).
if [[ "${1:-}" == "--check" ]] || [[ "$(date +%u)" == "7" ]]; then
    echo "[${TS}] Running data integrity check (10% subset)..." >&2
    restic check --read-data-subset=10% 2>&1
fi

echo "[${TS}] Off-site backup complete." >&2
