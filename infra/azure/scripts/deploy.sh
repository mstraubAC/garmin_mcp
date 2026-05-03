#!/usr/bin/env bash
# Deploy the Entra app registration and emit the env-file snippet the MCP
# server needs. Run from the infra/azure directory:
#
#   ./scripts/deploy.sh prod        # uses parameters/prod.bicepparam
#   ./scripts/deploy.sh dev         # uses parameters/dev.bicepparam
#
# Requires: az CLI logged in (`az login`) with Application.ReadWrite.OwnedBy
# (or higher) granted to the signed-in user.

set -euo pipefail

ENV_NAME="${1:-prod}"
PARAM_FILE="parameters/${ENV_NAME}.bicepparam"
DEPLOYMENT_NAME="garmin-mcp-${ENV_NAME}-$(date +%Y%m%d%H%M%S)"
SECRET_DAYS="${SECRET_DAYS:-90}"

if [[ ! -f "${PARAM_FILE}" ]]; then
    echo "Parameter file not found: ${PARAM_FILE}" >&2
    exit 1
fi

LOCATION=$(grep -E "^param location" "${PARAM_FILE}" | sed -E "s/.*=\s*'([^']+)'.*/\1/")
echo "Deploying ${ENV_NAME} (location=${LOCATION})..." >&2

az deployment sub create \
    --name "${DEPLOYMENT_NAME}" \
    --location "${LOCATION}" \
    --template-file main.bicep \
    --parameters "${PARAM_FILE}" \
    --output none

OUTPUTS=$(az deployment sub show --name "${DEPLOYMENT_NAME}" --query properties.outputs -o json)
APP_ID=$(echo "${OUTPUTS}" | jq -r '.appId.value')
TENANT_ID=$(echo "${OUTPUTS}" | jq -r '.tenantId.value')

echo "App registration deployed:" >&2
echo "  appId    = ${APP_ID}" >&2
echo "  tenantId = ${TENANT_ID}" >&2

# Mint a fresh client secret. `--append` so existing secrets aren't revoked
# (important when re-running deploy.sh on an existing app).
echo "Creating client secret (valid ${SECRET_DAYS} days)..." >&2
SECRET_JSON=$(az ad app credential reset \
    --id "${APP_ID}" \
    --append \
    --display-name "deploy-$(date +%Y%m%d)" \
    --years 0 \
    --end-date "$(date -u -v +${SECRET_DAYS}d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+${SECRET_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)" \
    --output json)

CLIENT_SECRET=$(echo "${SECRET_JSON}" | jq -r '.password')

cat <<EOF

----- BEGIN garmin-mcp env snippet -----
ENTRA_TENANT_ID=${TENANT_ID}
ENTRA_CLIENT_ID=${APP_ID}
ENTRA_CLIENT_SECRET=${CLIENT_SECRET}
----- END garmin-mcp env snippet -----

Append (or replace) these three lines in /etc/garmin-mcp/env on your VPS,
then restart the garmin-mcp container.

The secret value above is shown ONCE. Re-running this script appends a NEW
secret without revoking the old one — safe for rotation, but remember to
prune old secrets via 'rotate-secret.sh'.
EOF
