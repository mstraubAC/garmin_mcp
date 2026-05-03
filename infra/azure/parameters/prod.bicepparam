// Production parameters. Adjust `publicUrl` to your hostname before deploying.
//
// Run:
//   az deployment tenant create \
//     --location westeurope \
//     --template-file ../main.bicep \
//     --parameters prod.bicepparam

using '../main.bicep'

param location = 'westeurope'
param publicUrl = 'https://garmin-mcp.example.com'
param displayName = 'Garmin MCP Server'
param uniqueName = 'garmin-mcp-server'

// Optional: restrict sign-in to listed Entra user object IDs. Leave [] to
// allow any user in your tenant.
param assignedUserObjectIds = []
