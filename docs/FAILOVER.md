# AbstractFinance Failover Procedure

## Quick Start

**Automated failover** (recommended):
```bash
./scripts/failover.sh           # Interactive failover
./scripts/failover.sh --force   # Non-interactive (for emergencies)
```

**Automated state sync** (run periodically to keep standby warm):
```bash
./scripts/sync_state.sh   # Run from primary or standby
```

## Server Infrastructure

| Role | Hostname | IP Address | Location | WireGuard IP |
|------|----------|------------|----------|--------------|
| Primary (staging) | AbstractFinance-staging | 94.130.228.55 | nbg1 | 10.0.0.1 |
| Standby | abstractfinance-standby | 46.224.46.117 | fsn1 | 10.0.0.2 |

## WireGuard Tunnel

Both servers are connected via WireGuard VPN on UDP port 51820:
- Primary: 10.0.0.1/24
- Standby: 10.0.0.2/24

Test connectivity:
```bash
# From primary
ping 10.0.0.2

# From standby
ping 10.0.0.1
```

## Prerequisites on Standby

The standby server has:
- Docker and Docker Compose installed
- AbstractFinance repo cloned to `/srv/abstractfinance`
- WireGuard configured and running

Missing items that must be copied during failover:
- `.env` file with credentials
- `state/` directory with position state (if preserving continuity)

## Failover Procedure

### 1. Assess the Situation

Check if the primary is truly unreachable:
```bash
# Ping public IP
ping 94.130.228.55

# Check WireGuard
ping 10.0.0.1

# SSH attempt
ssh root@94.130.228.55 "docker ps"
```

### 2. Graceful Failover (Primary Still Accessible)

If the primary is accessible but needs to be taken offline:

```bash
# On PRIMARY: Stop trading cleanly
ssh root@94.130.228.55 "cd /srv/abstractfinance && docker compose down"

# Copy latest state and env to standby
scp root@94.130.228.55:/srv/abstractfinance/.env root@46.224.46.117:/srv/abstractfinance/.env
scp -r root@94.130.228.55:/srv/abstractfinance/state root@46.224.46.117:/srv/abstractfinance/

# On STANDBY: Start the stack
ssh root@46.224.46.117 "cd /srv/abstractfinance && docker compose up -d"
```

### 3. Emergency Failover (Primary Unreachable)

If the primary is unreachable:

```bash
# Copy the .env file from your local backup
scp /path/to/backup/.env root@46.224.46.117:/srv/abstractfinance/.env

# Or create a new .env with stored credentials
ssh root@46.224.46.117 "cat > /srv/abstractfinance/.env << 'EOF'
IBKR_USERNAME=your_username
IBKR_PASSWORD=your_password
IBKR_TOTP_KEY=your_totp_key
TRADING_MODE=paper
DB_PASSWORD=your_db_password
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
GRAFANA_PASSWORD=your_grafana_password
EOF
chmod 600 /srv/abstractfinance/.env"

# Start the stack
ssh root@46.224.46.117 "cd /srv/abstractfinance && docker compose up -d"
```

### 4. Verify Failover Success

```bash
# Check all containers are running
ssh root@46.224.46.117 "docker ps"

# Check IB Gateway connection (wait ~3 minutes for TOTP auth)
ssh root@46.224.46.117 "docker logs ibgateway 2>&1 | tail -50"

# Verify trading engine health
curl http://46.224.46.117:8080/health

# Check Grafana dashboard
# Open: http://46.224.46.117:3000
```

### 5. Post-Failover Tasks

1. **Update monitoring**: Point external uptime monitors to new IP
2. **Notify stakeholders**: Send Telegram/email about failover
3. **Investigate root cause**: Analyze what caused primary failure
4. **Plan recovery**: Prepare to restore primary or make standby permanent

## Failback Procedure

Once the primary is recovered:

```bash
# On STANDBY: Stop trading
ssh root@46.224.46.117 "cd /srv/abstractfinance && docker compose down"

# Copy state back to primary
scp -r root@46.224.46.117:/srv/abstractfinance/state root@94.130.228.55:/srv/abstractfinance/

# On PRIMARY: Start the stack
ssh root@94.130.228.55 "cd /srv/abstractfinance && docker compose up -d"

# Verify primary is operational
curl http://94.130.228.55:8080/health
```

## Important Notes

### IBKR Session Limitations

- **Only one concurrent IBKR session allowed per account**
- The old session must be terminated before the new one can connect
- If primary is unreachable, wait up to 5 minutes for session timeout
- Or use IBKR Account Management to forcibly disconnect

### Data Continuity

- PostgreSQL data is NOT replicated between servers
- Each server has independent `state/` directory
- For true data continuity, consider:
  - Regular PostgreSQL backups
  - Periodic state file sync via rsync

### WireGuard Maintenance

Check WireGuard status:
```bash
wg show
```

Restart WireGuard if needed:
```bash
systemctl restart wg-quick@wg0
```

## Automated State Sync (Warm Standby)

To keep the standby server ready for fast failover, set up automatic state sync:

```bash
# On PRIMARY server, add to crontab:
crontab -e

# Add this line (runs every 4 hours):
0 */4 * * * /srv/abstractfinance/scripts/sync_state.sh >> /var/log/abstractfinance/state_sync.log 2>&1
```

This syncs:
- `state/` directory (portfolio positions, trade history)
- `.env` file (credentials)
- Latest code from git
- Pre-pulls Docker images

## Automated Health Checks

The trading engine exposes `/health` on port 8080. External monitors should check:
- Primary: http://94.130.228.55:8080/health
- Standby: http://46.224.46.117:8080/health (when active)

Prometheus metrics available at:
- Primary: http://94.130.228.55:9090
- Grafana: http://94.130.228.55:3000

## Contact & Escalation

In case of failover:
1. Telegram alerts are sent automatically via Alertmanager
2. Check Grafana dashboard for current state
3. Review trading-engine logs for errors
