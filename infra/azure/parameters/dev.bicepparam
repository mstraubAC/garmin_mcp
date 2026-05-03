// Local-development parameters. Same Entra app, different redirect URI so the
// stack can be tested end-to-end against http://localhost:8000 without
// touching the production app registration.

using '../main.bicep'

param location = 'westeurope'
param resourceGroupName = 'rg-garmin-mcp-dev'
param publicUrl = 'http://localhost:8000'
param displayName = 'Garmin MCP Server (dev)'
param uniqueName = 'garmin-mcp-server-dev'
param assignedUserObjectIds = []
