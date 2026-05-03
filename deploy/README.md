# Deployment (Docker Compose on a small VPS)

This directory contains everything needed to bring the Garmin MCP server up
behind Caddy on a Linux box with Docker installed. Two services, one
public hostname, automatic Let's Encrypt TLS.

## Layout

```
deploy/
├── Dockerfile           multi-stage build, non-root user, healthcheck
├── docker-compose.yml   garmin-mcp + caddy services + named volumes
├── Caddyfile            TLS termination + reverse proxy
├── env.example          annotated env template (copy to /etc/garmin-mcp/env)
├── backup.sh            on-demand SQLite snapshot
└── README.md            this file
```

## Prerequisites

| Need | How |
|---|---|
| A small Linux VPS | Hetzner CX11 / CAX11 is plenty for personal use |
| Docker + Compose v2 | `curl -fsSL https://get.docker.com \| sh` |
| A DNS A/AAAA record | Point your hostname (e.g. `garmin-mcp.example.com`) at the VPS IP |
| Inbound 80/443 open | `ufw allow 80,443/tcp` |
| Entra app registration | See `infra/azure/` — `./scripts/deploy.sh prod` prints the env snippet |

## First-time setup

1. **Clone the repo on the VPS** (or `rsync` it; only `deploy/` and the
   project root are needed for the build context).

   ```bash
   git clone https://github.com/<you>/garmin_mcp.git /opt/garmin-mcp
   cd /opt/garmin-mcp
   ```

2. **Edit the Caddyfile** — replace `garmin-mcp.example.com` with your
   hostname and the global `email` with yours.

   ```bash
   $EDITOR deploy/Caddyfile
   ```

3. **Create the env file** at `/etc/garmin-mcp/env`. Generate the secrets
   inline; the values are shown once and never logged.

   ```bash
   sudo install -d -m 700 /etc/garmin-mcp
   sudo install -m 600 -o root -g root /dev/null /etc/garmin-mcp/env

   sudo bash -c 'cat > /etc/garmin-mcp/env <<EOF
   MCP_PUBLIC_URL=https://garmin-mcp.example.com
   JWT_SIGNING_KEY=$(openssl rand -base64 32)
   GARMIN_MCP_DATA_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   ENTRA_TENANT_ID=<from infra/azure/scripts/deploy.sh>
   ENTRA_CLIENT_ID=<from infra/azure/scripts/deploy.sh>
   ENTRA_CLIENT_SECRET=<from infra/azure/scripts/deploy.sh>
   EOF'

   sudo chmod 600 /etc/garmin-mcp/env
   ```

   **Back this file up.** Losing `GARMIN_MCP_DATA_KEY` makes every
   stored Garmin token unreadable — every user has to re-onboard.

4. **Build + start** the stack.

   ```bash
   cd /opt/garmin-mcp/deploy
   docker compose up -d --build
   docker compose ps                         # both services healthy?
   docker compose logs -f garmin-mcp         # watch for "session manager started"
   ```

5. **Smoke test** the public endpoint.

   ```bash
   curl -i https://garmin-mcp.example.com/healthz
   # → HTTP/2 200 with {"status":"ok"}
   curl -s https://garmin-mcp.example.com/.well-known/oauth-protected-resource/mcp | jq
   # → JSON with `authorization_servers` pointing back at your hostname
   ```

6. **Add the connector in a Claude app**: paste
   `https://garmin-mcp.example.com/mcp` as a custom MCP server. Claude
   walks you through Entra sign-in, then redirects to `/onboard` for the
   one-time Garmin login.

## Day-2 operations

### View logs

```bash
docker compose logs -f garmin-mcp        # uvicorn + app logs
docker compose logs -f caddy             # access logs + cert renewals
```

The structured audit log (every `register`, `authorize`, `token`, `onboard`)
lives inside the `garmin-mcp-logs` volume:

```bash
docker compose exec garmin-mcp tail -f /var/log/garmin-mcp/audit-*.log
```

### Update to a new version

```bash
git -C /opt/garmin-mcp pull
docker compose -f /opt/garmin-mcp/deploy/docker-compose.yml up -d --build
```

The schema upgrade is idempotent (`CREATE TABLE IF NOT EXISTS` + a
forward-compat version bump in `storage.py`).

### Rotate the Entra client secret

See `infra/azure/scripts/rotate-secret.sh`. Workflow: mint, paste into
`/etc/garmin-mcp/env`, `docker compose up -d garmin-mcp` to pick up the
new value, then `--prune` the old one in Azure.

### Backup

```bash
sudo /opt/garmin-mcp/deploy/backup.sh
```

Snapshots the SQLite database via SQLite's online backup API (consistent
point-in-time copy that doesn't block writers). Defaults to
`/var/backups/garmin-mcp/`. Add to root crontab for nightly:

```cron
0 3 * * * /opt/garmin-mcp/deploy/backup.sh >> /var/log/garmin-mcp-backup.log 2>&1
```

A real off-site backup story (restic to S3 / B2 / etc.) lives in step 9.

### Lockdown mode (optional)

Set `MCP_REGISTRATION_TOKEN=<random>` in the env file and restart:

```bash
docker compose up -d garmin-mcp
```

Now `/register` requires `Authorization: Bearer <that token>`. Useful
during early rollout when you want to manually approve every device that
registers. Remove the env var to lift it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `curl /healthz` returns 502 | garmin-mcp container not healthy yet — check `docker compose logs garmin-mcp` |
| Caddy can't get a cert | DNS not pointed yet, or port 80 not reachable from outside (Let's Encrypt does HTTP-01) |
| Claude says "couldn't connect to MCP server" | `MCP_PUBLIC_URL` mismatch with the actual hostname; re-check Caddyfile + env file agree |
| `/onboard` shows "Invalid email or password" | Garmin password was wrong, or Garmin temporarily flagged the IP — wait a few minutes |
| Logs show `failed to decrypt Garmin token` | `GARMIN_MCP_DATA_KEY` was rotated; restore the previous key from backup |

## Optional: Tailscale Funnel instead of public 443

If you'd rather not expose 443 to the internet, replace Caddy's `ports:`
block with a Tailscale Funnel (or sidecar). The auth + onboarding flow
works identically — Funnel just changes the ingress path.
