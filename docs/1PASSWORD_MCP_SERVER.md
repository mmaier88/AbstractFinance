# 1Password MCP Server Documentation

## Overview

The 1Password MCP Server provides a secure HTTP API for retrieving secrets from 1Password. It eliminates the need to store plaintext credentials in configuration files.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           YOUR LOCAL MACHINE                                 │
│                                                                             │
│  ┌──────────────────┐                      ┌──────────────────────────────┐ │
│  │ Claude Code      │                      │ ~/.claude/launch-claude.sh   │ │
│  │ (with MCP tools) │◄────── starts ───────│ (fetches secrets at launch)  │ │
│  └──────────────────┘                      └──────────────┬───────────────┘ │
│                                                           │                  │
└───────────────────────────────────────────────────────────┼──────────────────┘
                                                            │
                                                   HTTP (port 8080)
                                                            │
                                                            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      1Password MCP Server (91.99.97.249)                     │
│                                                                             │
│  ┌──────────────┐      ┌──────────────┐      ┌────────────────────────────┐│
│  │   Caddy      │      │  FastAPI     │      │  1Password Python SDK      ││
│  │ (port 8080)  │─────►│ (port 8000)  │─────►│  (connects to 1PW cloud)   ││
│  └──────────────┘      └──────────────┘      └────────────────────────────┘│
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                                            │
                                                   HTTPS (encrypted)
                                                            │
                                                            ▼
                                              ┌─────────────────────────┐
                                              │   1Password Cloud       │
                                              │   (1password.eu)        │
                                              └─────────────────────────┘
```

## Server Details

| Property | Value |
|----------|-------|
| **Server Name** | 1Password-MCP |
| **IP Address** | 91.99.97.249 |
| **Port** | 8080 (HTTP) |
| **Hetzner Server ID** | 115458524 |
| **Server Type** | CX22 |
| **OS** | Debian 12 |
| **Location** | nbg1 (Nuremberg) |

## API Endpoints

### Health Check
```bash
curl http://91.99.97.249:8080/health
# Response: {"status":"ok"}
```

### List Vaults
```bash
curl -H "Authorization: Bearer $API_KEY" http://91.99.97.249:8080/vaults
# Response: {"vaults":[{"id":"...","name":"Ai"}]}
```

### Get Secret (GET)
```bash
curl -H "Authorization: Bearer $API_KEY" \
  "http://91.99.97.249:8080/secret/{vault}/{item}/{field}"

# Example:
curl -H "Authorization: Bearer $API_KEY" \
  "http://91.99.97.249:8080/secret/Ai/hetzner-cloud/token"
# Response: {"value":"nv8ptQKJzK..."}
```

### Get Secret (POST)
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"vault":"Ai","item":"hetzner-cloud","field":"token"}' \
  http://91.99.97.249:8080/secret
```

## Authentication

All endpoints (except `/health`) require a Bearer token:

```
Authorization: Bearer <API_KEY>
```

The API key is stored in 1Password: `Ai/mcp-api-key/password`

## Secrets Stored in "Ai" Vault

| Item | Field | Description | Used By |
|------|-------|-------------|---------|
| `hetzner-cloud` | `token` | Hetzner Cloud API token | hetzner-mcp |
| `hummingbot` | `password` | Hummingbot API password | hummingbot-mcp |
| `supabase` | `token` | Supabase bearer token | supabase MCP |
| `mcp-api-key` | `password` | API key for this MCP server | Authentication |

## Local Configuration

### Claude Code Config (`~/.claude/config.json`)

The config uses environment variable references instead of hardcoded secrets:

```json
{
  "mcpServers": {
    "hetzner-mcp": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.12/bin/mcp-hetzner",
      "env": {
        "HCLOUD_TOKEN": "${HCLOUD_TOKEN}"
      }
    }
  }
}
```

### Launch Script (`~/.claude/launch-claude.sh`)

This script fetches secrets from the MCP server and sets environment variables:

```bash
#!/bin/bash
MCP_SERVER="http://91.99.97.249:8080"
API_KEY="<stored-in-1password>"

# Fetch secrets
export HCLOUD_TOKEN=$(curl -s -H "Authorization: Bearer $API_KEY" \
  "$MCP_SERVER/secret/Ai/hetzner-cloud/token" | jq -r '.value')

# Launch Claude
exec claude "$@"
```

## Server Configuration

### Service File (`/etc/systemd/system/mcp-server.service`)

```ini
[Unit]
Description=1Password MCP Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mcp-server
Environment=OP_SERVICE_ACCOUNT_TOKEN=<1password-service-account-token>
Environment=MCP_API_KEY=<api-key>
ExecStart=/usr/bin/python3 /opt/mcp-server/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Caddy Config (`/etc/caddy/Caddyfile`)

```
:8080 {
    reverse_proxy localhost:8000
    log {
        output file /var/log/caddy/mcp-server.log
    }
}
```

## Firewall Rules

The server uses the `abstractfinance-firewall` with these rules:
- Port 22: SSH
- Port 443: HTTPS (for future TLS)
- Port 8080: MCP API
- ICMP: Ping

## Management Commands

### Check Service Status
```bash
ssh root@91.99.97.249 "systemctl status mcp-server"
```

### View Logs
```bash
ssh root@91.99.97.249 "journalctl -u mcp-server -f"
```

### Restart Service
```bash
ssh root@91.99.97.249 "systemctl restart mcp-server"
```

### Test API
```bash
curl http://91.99.97.249:8080/health
```

## Security Considerations

### What's Secured
- ✅ Secrets stored in 1Password (encrypted at rest)
- ✅ API requires authentication token
- ✅ No plaintext secrets in Claude Code config
- ✅ Secrets fetched just-in-time at launch

### Current Limitations
- ⚠️ HTTP only (not HTTPS) - acceptable for internal use
- ⚠️ API key stored in launch script - could use env var
- ⚠️ MCP server has root access on its host

### Future Improvements
- Add TLS with proper certificate (requires domain)
- Rate limiting
- Audit logging
- IP allowlisting

## Troubleshooting

### MCP Server Not Responding
```bash
# Check if service is running
ssh root@91.99.97.249 "systemctl status mcp-server"

# Check logs
ssh root@91.99.97.249 "journalctl -u mcp-server -n 50"

# Restart service
ssh root@91.99.97.249 "systemctl restart mcp-server"
```

### Authentication Errors
- Verify API key is correct
- Check Authorization header format: `Bearer <token>`
- Ensure no trailing whitespace in token

### Secret Not Found
- Verify vault name (case-sensitive): `Ai`
- Verify item name exists in 1Password
- Verify field name (usually `token` or `password`)

## Related Documentation

- [IBKR Setup Guide](./IBKR_SETUP.md)
- [1Password Vaults](../infra/1password/vaults-and-groups.md)
