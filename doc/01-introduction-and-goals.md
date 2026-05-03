# 1. Introduction and goals

## What this is

A Model Context Protocol (MCP) server that exposes a user's Garmin Connect
data — activities, sleep, training metrics, nutrition, workouts, devices —
as MCP tools that Claude (or any other MCP client) can call. The server is
deployable behind a public hostname and protects access with OAuth 2.1 +
Microsoft Entra ID, so the same instance can serve multiple users from a
single tenant, each authenticated as themselves and reading *their own*
Garmin data.

The original [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)
runs over stdio and assumes one user with credentials in env vars. This
fork adds the network and identity layers needed to host it as a remote
MCP server reachable from the Claude apps (mobile, web, desktop).

## Top use cases

1. **Coach a workout from anywhere.** Open Claude on your phone after a
   run; ask "how does today's pace compare to my last 5K?". Claude calls
   `get_activity_details` + `get_user_summary` against your Garmin data
   without you ever leaving the chat.

2. **Plan a training week.** From a browser at the kitchen table, ask
   Claude to look at recent training load, sleep score, and HRV, then
   build a workout for tomorrow and `upload_workout` it back to Garmin.

3. **Family member shares the same server.** A second Entra user signs in
   to the same MCP URL, completes the one-time onboarding with their own
   Garmin credentials, and queries their own data. Neither user ever
   sees the other's tokens or activities.

## Quality goals

Ranked — when a trade-off shows up, the higher item wins.

| # | Goal | Concrete scenario |
|---|---|---|
| 1 | **Per-user isolation** — never leak data across users | Two Entra users on the same instance can never see each other's Garmin data, even if one's session is compromised |
| 2 | **Auth that actually works in the Claude apps** | Adding the server in Claude mobile triggers a normal OAuth browser dance; no manual API key fiddling |
| 3 | **Operable by one person** | One person can deploy, monitor, back up, and rotate secrets without on-call rotation; recovery from VPS loss is < 1 hour from a backup |
| 4 | **Defensible against the obvious attacks** | Public DCR endpoint can't be flooded into uselessness; encrypted Garmin tokens at rest; signed access tokens with short TTL |
| 5 | **Tools stay decoupled from auth** | A new MCP tool can be added by editing one module; tool code never touches OAuth, JWT, or per-user lookup directly |

## Stakeholders

| Role | Interest |
|---|---|
| **Self-hoster** (you) | Wants the system to stay up without babysitting; wants secrets and backups handled |
| **Authenticated user** | Wants to "add the server in Claude" and start asking questions about *their* Garmin data; wants the onboarding flow to be obvious |
| **Garmin Connect** | Cares about rate limits and ToS — multi-user means we have to spread load and not abuse a single account |
| **Microsoft Entra ID** | Issues the user identity; sets the rules for app registration, redirect URIs, and consent |
| **MCP spec** | Defines the wire protocol, the auth model (OAuth 2.1 + DCR), and the resource-server metadata Claude expects |
