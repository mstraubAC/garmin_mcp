#!/usr/bin/env bash
# Mint a new client secret, print the env snippet, and (after a confirmation)
# delete all OTHER existing secrets on the app. Run with the app's appId:
#
#   ./scripts/rotate-secret.sh <appId>
#
# Workflow:
#   1. Run this script — write the new secret onto the VPS env file
#   2. Restart the container so it picks up the new secret
#   3. Re-run with --prune to delete the old secret(s)

set -euo pipefail

APP_ID="${1:-}"
PRUNE="${2:-}"
SECRET_DAYS="${SECRET_DAYS:-90}"

if [[ -z "${APP_ID}" ]]; then
    echo "Usage: $0 <appId> [--prune]" >&2
    exit 1
fi

if [[ "${PRUNE}" == "--prune" ]]; then
    echo "Listing existing secrets on ${APP_ID}..." >&2
    KEY_IDS=$(az ad app credential list --id "${APP_ID}" --query "[].keyId" -o tsv)
    LATEST=$(az ad app credential list --id "${APP_ID}" \
        --query "max_by([], &endDateTime).keyId" -o tsv)
    echo "Latest secret: ${LATEST}" >&2
    for kid in ${KEY_IDS}; do
        if [[ "${kid}" == "${LATEST}" ]]; then continue; fi
        echo "Deleting secret ${kid}..." >&2
        az ad app credential delete --id "${APP_ID}" --key-id "${kid}"
    done
    echo "Done. Only the latest secret remains." >&2
    exit 0
fi

echo "Minting new client secret (valid ${SECRET_DAYS} days)..." >&2
SECRET_JSON=$(az ad app credential reset \
    --id "${APP_ID}" \
    --append \
    --display-name "rotate-$(date +%Y%m%d)" \
    --years 0 \
    --end-date "$(date -u -v +${SECRET_DAYS}d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+${SECRET_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)" \
    --output json)

CLIENT_SECRET=$(echo "${SECRET_JSON}" | jq -r '.password')

cat <<EOF

----- NEW SECRET (rotate /etc/garmin-mcp/env on VPS) -----
ENTRA_CLIENT_SECRET=${CLIENT_SECRET}
----------------------------------------------------------

Next steps:
  1. Update ENTRA_CLIENT_SECRET on the VPS env file
  2. Restart the garmin-mcp container so the new secret takes effect
  3. After verifying it works, prune the old secret:
       $0 ${APP_ID} --prune
EOF
