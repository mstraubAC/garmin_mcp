# Azure infrastructure (Microsoft Entra ID)

Bicep + helper scripts that provision the Microsoft Entra app registration the
Garmin MCP OAuth proxy uses to authenticate users from your Microsoft 365
tenant. The app is single-tenant; sign-in is restricted to your tenant
(optionally further restricted to specific users).

## What gets created

- **Resource group** (e.g. `rg-garmin-mcp`) — only used as a Bicep deployment
  scope; the Entra resources are tenant-scoped and not billed against the RG.
- **App registration** (`Microsoft.Graph/applications`) with:
  - `signInAudience: AzureADMyOrg` (single-tenant)
  - Web redirect URI: `${publicUrl}/callback`
  - Delegated MS Graph permissions: `openid`, `profile`, `email`
  - No client secret declared in Bicep (managed by `scripts/deploy.sh`, see
    [Why secrets aren't in Bicep](#why-secrets-arent-in-bicep))
- **Service principal** (`Microsoft.Graph/servicePrincipals`) with
  `appRoleAssignmentRequired = true` if `assignedUserObjectIds` is non-empty.

## Prerequisites

- Azure subscription in your O365 tenant
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
  with [Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/install)
  v0.36.1 or newer (`az bicep upgrade`)
- `jq` for the deploy/rotate scripts
- An Entra account with at least `Application.ReadWrite.OwnedBy` on the tenant

## Deploy

```bash
cd infra/azure

# Edit the parameter file — at minimum set publicUrl
$EDITOR parameters/prod.bicepparam

# One-shot deploy: creates the RG, app, SP, and emits a fresh client secret
az login --tenant <your-tenant-id>
./scripts/deploy.sh prod
```

The script prints an env-file snippet at the end:

```
----- BEGIN garmin-mcp env snippet -----
ENTRA_TENANT_ID=...
ENTRA_CLIENT_ID=...
ENTRA_CLIENT_SECRET=...
----- END garmin-mcp env snippet -----
```

Copy those three lines into `/etc/garmin-mcp/env` on your VPS (mode `0600`,
owned by the container user). The secret value is shown **once** by the Graph
API — there is no way to retrieve it later.

## Restrict sign-in to specific users

By default any user in your tenant can sign in to the app. To restrict:

1. Get user object IDs (`az ad user show --id user@example.com --query id -o tsv`)
2. Add them to `assignedUserObjectIds` in `parameters/prod.bicepparam`
3. Re-run `./scripts/deploy.sh prod`

This sets `appRoleAssignmentRequired=true` on the service principal but does
**not** create the assignment records themselves (see the Bicep comment for
why). After the deploy, assign each user via the portal (Enterprise
applications → Garmin MCP Server → Users and groups) or with:

```bash
az rest --method post \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/<sp-object-id>/appRoleAssignments" \
  --body '{
    "principalId": "<user-object-id>",
    "resourceId": "<sp-object-id>",
    "appRoleId": "00000000-0000-0000-0000-000000000000"
  }'
```

## Rotate the client secret

Entra secrets expire (default 90 days). Workflow:

```bash
# 1. Mint a new secret (without revoking the old one)
./scripts/rotate-secret.sh <appId>

# 2. Paste the new ENTRA_CLIENT_SECRET into /etc/garmin-mcp/env on the VPS
# 3. Restart the container — verify sign-in still works
# 4. Prune the old secret
./scripts/rotate-secret.sh <appId> --prune
```

Both phases are non-destructive on their own: the rotation step adds a new
secret, the prune step removes everything except the latest. If the new
deploy is broken, skip prune and roll back to the previous secret in the env
file — the old secret is still valid until you prune.

## Verify the deploy

```bash
# Confirm the app is visible
az ad app show --id <appId> --query "{name:displayName, audience:signInAudience, redirects:web.redirectUris}"

# Diff next deployment vs. live state — should show no changes on a re-run
az deployment sub what-if \
  --location westeurope \
  --template-file main.bicep \
  --parameters parameters/prod.bicepparam
```

A clean `what-if` (`No changes`) is the regression test for this directory:
re-running `deploy.sh` is idempotent except for the secret it mints.

## Why secrets aren't in Bicep

The Microsoft Graph Bicep extension can declare `passwordCredentials` on an
app, but the generated `secretText` is only returned during the initial create
call — it isn't a recoverable Bicep output, and re-deploys would either
revoke or no-op silently depending on the diff. Managing secrets via
`az ad app credential reset` from the deploy script keeps the value out of
both Bicep state and CI logs (it only appears on stdout once, ready to be
pasted into the env file on the VPS).

## Fallback: deploy without the Bicep extension

If the Microsoft Graph Bicep extension isn't available in your environment
(it was preview at time of writing), the same app can be created with raw az
CLI commands:

```bash
APP_ID=$(az ad app create \
  --display-name "Garmin MCP Server" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris https://garmin-mcp.example.com/callback \
  --required-resource-accesses '[{
    "resourceAppId": "00000003-0000-0000-c000-000000000000",
    "resourceAccess": [
      {"id": "37f7f235-527c-4136-accd-4a02d197296e", "type": "Scope"},
      {"id": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0", "type": "Scope"},
      {"id": "14dad69e-099b-42c9-810b-d002981feec1", "type": "Scope"}
    ]
  }]' \
  --query appId -o tsv)

az ad sp create --id "${APP_ID}"
az ad app credential reset --id "${APP_ID}" --years 0 --end-date "$(date -u -v +90d +%Y-%m-%dT%H:%M:%SZ)"
```

This is an escape hatch — prefer the Bicep flow so the configuration stays
declarative and review-able.
