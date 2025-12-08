# Production Hardening Guide

This document outlines the infrastructure improvements needed before deploying with real capital.

## Current State (Paper Trading)

- Single Hetzner VPS running all components
- Root user deployment
- `.env` file for secrets
- Basic Prometheus/Grafana monitoring
- Manual deployments via SSH

**This is acceptable for paper trading but NOT for production.**

---

## 1. High Availability / Redundancy

### Problem
Single VPS = single point of failure. If the Hetzner box dies, trading stops.

### Solution Options

#### Option A: Warm Standby (Recommended for <$1M AUM)
```
┌─────────────────────┐     ┌─────────────────────┐
│  PRIMARY (CX31)     │     │  STANDBY (CX21)     │
│  - IB Gateway       │     │  - IB Gateway       │
│  - Trading Engine   │     │  - Trading Engine   │
│  - PostgreSQL       │     │  - PostgreSQL replica│
│  - Monitoring       │     │  (cold standby)     │
└─────────────────────┘     └─────────────────────┘
         │                           │
         └─────── WireGuard ─────────┘
```

- Standby syncs config/state via rsync
- Manual failover (< 5 min RTO)
- Cost: ~€10/month extra

#### Option B: Split Architecture (Recommended for >$1M AUM)
```
┌─────────────────────┐     ┌─────────────────────┐
│  BOX A (Gateway)    │     │  BOX B (Engine)     │
│  - IB Gateway       │◄───►│  - Trading Engine   │
│  - Prometheus       │ WG  │  - PostgreSQL       │
│  - Grafana          │     │  - Scheduler        │
└─────────────────────┘     └─────────────────────┘
```

- Gateway isolation (security boundary)
- Engine can reconnect to standby Gateway
- Cost: ~€20/month extra

### Implementation Checklist
- [x] Provision standby VPS (abstractfinance-standby @ 46.224.46.117, fsn1)
- [x] Set up WireGuard tunnel (10.0.0.1 <-> 10.0.0.2)
- [ ] Configure PostgreSQL streaming replication
- [x] Document failover procedure (docs/FAILOVER.md)
- [ ] Test failover quarterly

---

## 2. IB Gateway Operations Lifecycle

### Problem
IB Gateway requires:
- Periodic restarts (sessions expire)
- Weekly maintenance windows (Sunday)
- Re-authentication after disconnects

### Solution

#### Scheduled Restart Window
Add to crontab on server:
```bash
# /etc/cron.d/ibgateway-maintenance
# Restart IB Gateway every Sunday at 22:00 UTC (before IBKR maintenance)
0 22 * * 0 root /srv/abstractfinance/scripts/restart_gateway.sh
```

Create restart script:
```bash
#!/bin/bash
# /srv/abstractfinance/scripts/restart_gateway.sh

set -e
cd /srv/abstractfinance

# Log the restart
echo "$(date): Scheduled Gateway restart" >> /var/log/ibgateway-maintenance.log

# Graceful restart
docker compose stop ibgateway
sleep 10
docker compose up -d ibgateway

# Wait for healthy
sleep 120

# Verify connection
docker compose exec -T trading-engine python -c "
from ib_insync import IB
ib = IB()
ib.connect('ibgateway', 4004, clientId=99)
print('Gateway healthy')
ib.disconnect()
"

if [ $? -eq 0 ]; then
    echo "$(date): Gateway restart successful" >> /var/log/ibgateway-maintenance.log
else
    echo "$(date): Gateway restart FAILED" >> /var/log/ibgateway-maintenance.log
    # Send alert
    curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=ALERT: IB Gateway restart failed!"
fi
```

#### Auto-Reconnect Logic
Already implemented in `src/execution_ibkr.py`:
```python
# Connection with auto-reconnect
self.ib = IB()
self.ib.connect(host, port, clientId=client_id)

# Reconnect handler
def on_disconnect():
    logger.warning("IB Gateway disconnected, attempting reconnect...")
    for attempt in range(5):
        try:
            self.ib.connect(host, port, clientId=client_id)
            logger.info("Reconnected successfully")
            return
        except Exception as e:
            logger.error(f"Reconnect attempt {attempt+1} failed: {e}")
            time.sleep(30)
    # All retries failed - alert
    send_alert("CRITICAL: Cannot reconnect to IB Gateway")

self.ib.disconnectedEvent += on_disconnect
```

#### IBKR Maintenance Windows
- **Sunday 23:45 - Monday 00:45 UTC**: Weekly system restart
- **Daily 22:00-22:15 UTC**: Possible brief disconnects

Configure trading engine to:
1. Not place orders during maintenance windows
2. Expect disconnects and handle gracefully

### Implementation Checklist
- [x] Create restart script (scripts/restart_gateway.sh)
- [x] Add cron job for Sunday restarts (/etc/cron.d/abstractfinance-maintenance)
- [x] Verify auto-reconnect logic in engine
- [x] Add maintenance window awareness to scheduler
- [x] Set up Telegram alerts for disconnects

---

## 3. Security & Secrets Management

### Problem
- `.env` file contains plaintext credentials
- Deploying as root user
- Credentials visible in docs

### Immediate Fixes

#### File Permissions
```bash
# On server
chmod 600 /srv/abstractfinance/.env
chown root:root /srv/abstractfinance/.env

# Or better, create service user
useradd -r -s /bin/false abstractfinance
chown abstractfinance:abstractfinance /srv/abstractfinance/.env
```

#### Remove Credentials from Git
Ensure `.gitignore` contains:
```
.env
.env.*
*.pem
*.key
credentials.json
```

#### Sanitize Documentation
Never commit real:
- Account IDs
- Usernames
- API keys
- IP addresses (use placeholders)

### Long-term: Secret Manager

#### Option A: SOPS + Age (Simple)
```bash
# Encrypt .env
sops --encrypt --age $(cat ~/.age/key.txt | grep public | cut -d: -f2) \
    .env > .env.encrypted

# Decrypt on deploy
sops --decrypt .env.encrypted > .env
```

#### Option B: HashiCorp Vault (Enterprise)
```yaml
# docker-compose.yml addition
vault:
  image: vault:1.13
  cap_add:
    - IPC_LOCK
  environment:
    VAULT_DEV_ROOT_TOKEN_ID: "dev-token"
  ports:
    - "8200:8200"
```

#### Option C: Doppler (SaaS)
```bash
# Install Doppler CLI
doppler run -- docker compose up -d
```

### Implementation Checklist
- [x] Fix file permissions on `.env` (chmod 600)
- [x] Create non-root service user (abstractfinance)
- [x] Audit git history for leaked secrets (clean)
- [x] Choose and implement secret manager (1Password Business)
- [ ] Rotate IBKR password after any exposure

---

## 4. Deployment Hygiene

### Problem
- Manual SSH + `docker compose up`
- Running as root
- No version pinning
- No audit trail

### Solution

#### Create Service User
```bash
# On server
useradd -m -s /bin/bash abstractfinance
usermod -aG docker abstractfinance

# Set up SSH key for deploys
mkdir -p /home/abstractfinance/.ssh
cp /root/.ssh/authorized_keys /home/abstractfinance/.ssh/
chown -R abstractfinance:abstractfinance /home/abstractfinance/.ssh
chmod 700 /home/abstractfinance/.ssh
chmod 600 /home/abstractfinance/.ssh/authorized_keys
```

#### Pin Versions
```dockerfile
# Dockerfile - pin Python packages
FROM python:3.11.7-slim

# requirements.txt - pin versions
ib_insync==0.9.86
pandas==2.1.4
numpy==1.26.3
```

```yaml
# docker-compose.yml - pin images
services:
  ibgateway:
    image: gnzsnz/ib-gateway:10.19.2j  # Pin specific version
```

#### Systemd Service (Alternative to Docker Compose)
```ini
# /etc/systemd/system/abstractfinance.service
[Unit]
Description=AbstractFinance Trading Engine
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=abstractfinance
WorkingDirectory=/srv/abstractfinance
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose restart

[Install]
WantedBy=multi-user.target
```

#### GitHub Actions CI/CD
```yaml
# .github/workflows/deploy.yml
name: Deploy to Production

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4

      - name: Deploy to server
        uses: appleboy/ssh-action@v1.0.0
        with:
          host: ${{ secrets.SERVER_IP }}
          username: abstractfinance
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd /srv/abstractfinance
            git pull origin main
            docker compose build trading-engine
            docker compose up -d trading-engine

      - name: Verify deployment
        uses: appleboy/ssh-action@v1.0.0
        with:
          host: ${{ secrets.SERVER_IP }}
          username: abstractfinance
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            docker compose -f /srv/abstractfinance/docker-compose.yml ps
            docker compose -f /srv/abstractfinance/docker-compose.yml logs --tail=20 trading-engine
```

### Implementation Checklist
- [x] Create `abstractfinance` service user
- [x] Pin all Docker image versions
- [x] Pin all Python package versions
- [x] Set up GitHub Actions deployment
- [x] Document rollback procedure (docs/ROLLBACK.md)
- [ ] Add deployment audit logging

---

## 5. Monitoring Completeness

### Problem
Current monitoring tracks system metrics but not:
- Order rejections
- Connection state
- Strategy-level risk metrics
- PnL thresholds

### Solution

#### Trading Metrics to Add
```python
# src/metrics.py
from prometheus_client import Counter, Gauge, Histogram

# Connection metrics
ib_connection_state = Gauge('ib_connection_state', 'IB Gateway connection state (1=connected)')
ib_reconnect_count = Counter('ib_reconnect_total', 'Total IB reconnection attempts')
ib_last_heartbeat = Gauge('ib_last_heartbeat_timestamp', 'Last heartbeat from IB Gateway')

# Order metrics
orders_submitted = Counter('orders_submitted_total', 'Total orders submitted', ['instrument', 'side'])
orders_filled = Counter('orders_filled_total', 'Total orders filled', ['instrument', 'side'])
orders_rejected = Counter('orders_rejected_total', 'Total orders rejected', ['instrument', 'reason'])
order_latency = Histogram('order_fill_latency_seconds', 'Order fill latency')

# Risk metrics
portfolio_nav = Gauge('portfolio_nav_usd', 'Portfolio NAV in USD')
portfolio_gross_exposure = Gauge('portfolio_gross_exposure_usd', 'Gross exposure in USD')
portfolio_net_exposure = Gauge('portfolio_net_exposure_usd', 'Net exposure in USD')
portfolio_beta_spx = Gauge('portfolio_beta_spx', 'Portfolio beta to SPX')
portfolio_drawdown = Gauge('portfolio_drawdown_pct', 'Current drawdown percentage')
sleeve_pnl = Gauge('sleeve_pnl_usd', 'Sleeve PnL', ['sleeve'])
sleeve_weight = Gauge('sleeve_weight_pct', 'Sleeve weight', ['sleeve'])

# Pacing/rate limits
ib_pacing_violations = Counter('ib_pacing_violations_total', 'IB API pacing violations')
```

#### Alertmanager Rules
```yaml
# alertmanager/rules.yml
groups:
  - name: trading
    rules:
      # No heartbeat from engine
      - alert: EngineNoHeartbeat
        expr: time() - ib_last_heartbeat_timestamp > 300
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Trading engine has no heartbeat for 5+ minutes"

      # Gateway disconnected
      - alert: GatewayDisconnected
        expr: ib_connection_state == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "IB Gateway disconnected"

      # Drawdown threshold
      - alert: DrawdownThreshold
        expr: portfolio_drawdown_pct > 5
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Portfolio drawdown exceeds 5%"

      - alert: DrawdownCritical
        expr: portfolio_drawdown_pct > 8
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Portfolio drawdown exceeds 8% - consider emergency de-risk"

      # Order rejections
      - alert: HighOrderRejections
        expr: rate(orders_rejected_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High order rejection rate"

      # Pacing violations
      - alert: PacingViolations
        expr: rate(ib_pacing_violations_total[5m]) > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "IB API pacing violations detected"
```

#### Telegram Alert Integration
```python
# src/alerts.py
import httpx
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

async def send_telegram_alert(message: str, severity: str = "info"):
    """Send alert to Telegram."""
    emoji = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🚨",
    }.get(severity, "📢")

    text = f"{emoji} *AbstractFinance Alert*\n\n{message}"

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            }
        )
```

#### Grafana Dashboard Additions
Create panels for:
1. **Connection Status**: IB Gateway state, reconnect count, last heartbeat
2. **Order Flow**: Submitted vs filled vs rejected, fill latency histogram
3. **Risk Metrics**: NAV, gross/net exposure, beta, drawdown
4. **Sleeve Performance**: PnL by sleeve, weight drift from target
5. **System Health**: Pacing violations, error rates

### Implementation Checklist
- [x] Add Prometheus metrics to trading engine (src/metrics.py)
- [x] Configure Alertmanager rules (infra/alert-rules.yml)
- [x] Set up Telegram bot for alerts (infra/alertmanager.yml)
- [x] Create comprehensive Grafana dashboard (infra/grafana/provisioning/dashboards/)
- [ ] Test alert delivery end-to-end

---

## Production Readiness Checklist

### Before Going Live

#### Infrastructure
- [x] Standby VPS provisioned and tested (abstractfinance-standby @ fsn1)
- [x] WireGuard tunnel configured (10.0.0.1 <-> 10.0.0.2)
- [x] Failover procedure documented (docs/FAILOVER.md)
- [x] PostgreSQL backup script deployed (scripts/backup_postgres.sh)
- [x] PostgreSQL replication scripts available (scripts/setup_pg_replication.sh)

#### Operations
- [x] Gateway restart script deployed (scripts/restart_gateway.sh)
- [x] Cron job for Sunday maintenance (/etc/cron.d/abstractfinance-maintenance)
- [x] Auto-reconnect logic verified
- [x] Maintenance window handling in scheduler
- [x] Daily PostgreSQL backups (3:00 UTC cron)

#### Security
- [x] Non-root service user created (abstractfinance)
- [x] File permissions hardened (chmod 600 .env)
- [x] Secrets in proper manager (1Password Business)
- [x] Git history audited for leaks
- [ ] IBKR password rotated
- [x] Caddy SSL/TLS config available (infra/Caddyfile)

#### Deployment
- [x] All versions pinned (Docker images + Python packages)
- [x] CI/CD pipeline configured (GitHub Actions)
- [x] Rollback procedure documented (docs/ROLLBACK.md)
- [ ] Deployment audit logging enabled

#### Monitoring
- [x] All trading metrics exposed (src/metrics.py)
- [x] Execution metrics wired (orders/fills/latency in execution_ibkr.py)
- [x] Alert rules configured (infra/alert-rules.yml)
- [x] Telegram alerts working (via Alertmanager)
- [x] Grafana dashboards complete
- [ ] On-call rotation defined

#### Testing
- [ ] 60-day paper trading completed
- [ ] Failover tested
- [ ] Disaster recovery tested
- [ ] Alert escalation tested

---

## Appendix: Quick Commands

### Failover to Standby
```bash
# On standby server
cd /srv/abstractfinance
git pull origin main
docker compose up -d
```

### Emergency Stop
```bash
# Cancel all orders and flatten positions
docker compose exec trading-engine python -c "
from src.execution_ibkr import IBKRExecution
exec = IBKRExecution()
exec.cancel_all_orders()
exec.flatten_all_positions()
"
```

### Check Gateway Health
```bash
docker compose exec trading-engine python -c "
from ib_insync import IB
ib = IB()
ib.connect('ibgateway', 4004, clientId=99)
print('Connected:', ib.isConnected())
print('Server version:', ib.client.serverVersion())
ib.disconnect()
"
```

### View Recent Errors
```bash
docker compose logs trading-engine 2>&1 | grep -i "error\|failed\|rejected" | tail -50
```
