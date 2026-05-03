// Tenant-scope deployment of the Microsoft Entra app registration + service
// principal used by the Garmin MCP OAuth proxy.
//
// No Azure subscription or resource group is involved — Graph resources are
// tenant-scoped and the deployment is recorded against the tenant directly.
//
// Deploy with:
//   az deployment tenant create \
//     --location <location> \
//     --template-file main.bicep \
//     --parameters parameters/prod.bicepparam
//
// Outputs `appId`, `objectId`, `tenantId` are written to stdout — pipe them
// into your VPS env file via `scripts/deploy.sh`.

targetScope = 'tenant'

extension microsoftGraphV1

@description('Azure region recorded against the deployment metadata. Does not provision any regional resource.')
param location string = 'westeurope'

@description('Public URL where the MCP server will be reached. The OAuth callback URI is publicUrl + /callback.')
param publicUrl string

@description('Display name shown to users on the consent screen.')
param displayName string = 'Garmin MCP Server'

@description('Stable, immutable identifier used by Bicep to reconcile the app registration on re-deploys. Must be unique per environment (e.g. ...-prod, ...-dev) so a bare deploy without a parameter file fails loudly instead of clobbering an existing app.')
param uniqueName string

@description('Object IDs of users explicitly assigned to the app. Empty = all tenant users may sign in (subject to tenant policy).')
param assignedUserObjectIds array = []

// `location` is referenced so the parameter isn't flagged as unused; the
// value is consumed by `az deployment tenant create --location ...`, not by
// any resource declared here.
var deploymentLocation = location

// Microsoft Graph well-known IDs (constants — stable across all tenants).
var graphAppId = '00000003-0000-0000-c000-000000000000'
var scopeOpenId = '37f7f235-527c-4136-accd-4a02d197296e'
var scopeEmail = '64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0'
var scopeProfile = '14dad69e-099b-42c9-810b-d002981feec1'

resource app 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: displayName
  signInAudience: 'AzureADMyOrg'
  description: 'OAuth resource server for the Garmin MCP proxy.'
  web: {
    redirectUris: [
      '${publicUrl}/callback'
    ]
    implicitGrantSettings: {
      enableAccessTokenIssuance: false
      enableIdTokenIssuance: false
    }
  }
  requiredResourceAccess: [
    {
      resourceAppId: graphAppId
      resourceAccess: [
        { id: scopeOpenId, type: 'Scope' }
        { id: scopeEmail, type: 'Scope' }
        { id: scopeProfile, type: 'Scope' }
      ]
    }
  ]
}

resource sp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: app.appId
  // When true, only assigned users can sign in. The actual user assignments
  // are created out-of-band by `scripts/deploy.sh` (using `az rest`); the
  // appRoleAssignedTo Bicep type is not yet stable enough to declare here.
  appRoleAssignmentRequired: !empty(assignedUserObjectIds)
}

output appId string = app.appId
output objectId string = app.id
output servicePrincipalId string = sp.id
output tenantId string = tenant().tenantId
output deploymentLocation string = deploymentLocation
