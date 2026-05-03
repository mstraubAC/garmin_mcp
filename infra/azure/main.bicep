// Subscription-scope deployment that ensures the resource group exists and
// then provisions the Microsoft Entra ID app registration + service principal
// used by the Garmin MCP OAuth proxy.
//
// The resource group is only used as a Bicep deployment scope; the Graph
// resources themselves are tenant-scoped and not billed against the RG.
//
// Deploy with:
//   az deployment sub create \
//     --location <location> \
//     --template-file main.bicep \
//     --parameters parameters/prod.bicepparam
//
// Outputs `appId`, `objectId`, `tenantId` are written to stdout — pipe them
// into your VPS env file via `scripts/deploy.sh`.

targetScope = 'subscription'

@description('Azure region used for the (otherwise-empty) resource group that hosts the Graph deployment.')
param location string = 'westeurope'

@description('Resource group name. Created if it does not exist.')
param resourceGroupName string = 'rg-garmin-mcp'

@description('Public URL where the MCP server will be reached. The OAuth callback URI is publicUrl + /callback.')
param publicUrl string

@description('Display name shown to users on the consent screen.')
param displayName string = 'Garmin MCP Server'

@description('Stable, immutable identifier used by Bicep to reconcile the app registration on re-deploys.')
param uniqueName string = 'garmin-mcp-server'

@description('Object IDs of users explicitly assigned to the app. Empty = all tenant users may sign in (subject to tenant policy).')
param assignedUserObjectIds array = []

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

module entra 'modules/entra.bicep' = {
  scope: rg
  name: 'garmin-mcp-entra'
  params: {
    displayName: displayName
    uniqueName: uniqueName
    redirectUri: '${publicUrl}/callback'
    assignedUserObjectIds: assignedUserObjectIds
  }
}

output appId string = entra.outputs.appId
output objectId string = entra.outputs.objectId
output servicePrincipalId string = entra.outputs.servicePrincipalId
output tenantId string = subscription().tenantId
