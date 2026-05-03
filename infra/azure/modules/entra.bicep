// Microsoft Entra app registration + service principal for the Garmin MCP
// OAuth proxy. Single-tenant; OIDC scopes (openid, profile, email).
//
// Client secrets are NOT declared in Bicep. The Bicep extension cannot
// surface secret values back into a re-deploy, so secrets are managed
// out-of-band via `scripts/deploy.sh` (`az ad app credential reset`).

extension microsoftGraphV1

@description('Display name shown on the consent screen.')
param displayName string

@description('Stable identifier used by Bicep to reconcile this app on re-deploys. Immutable.')
param uniqueName string

@description('OAuth redirect URI (must be HTTPS in production).')
param redirectUri string

@description('Object IDs of users assigned to the app. When non-empty, sign-in is restricted to assigned users.')
param assignedUserObjectIds array

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
      redirectUri
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
  // are created out-of-band by `scripts/deploy.sh` (using `az ad app`); the
  // appRoleAssignedTo Bicep type is not yet stable enough to declare here.
  appRoleAssignmentRequired: !empty(assignedUserObjectIds)
}

output appId string = app.appId
output objectId string = app.id
output servicePrincipalId string = sp.id
